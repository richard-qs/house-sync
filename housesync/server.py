"""Loopback-only development application. Run with ``python -m housesync.server``."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import traceback
import uuid
import webbrowser
from contextlib import suppress
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from pydantic import ValidationError

from .changes import compare_revisions, export_change_report
from .documents import drawing_svg, export_documents
from .engine import ROOF_OVERHANG_MM, ROOF_THICKNESS_MM, SLAB_THICKNESS_MM, build_house, revision_for
from .layout import derive_layout
from .purchasing import UnknownPurchaseOrder, purchase_order_pdf, purchase_orders_zip, validate_purchase_order_statuses
from .quote_documents import export_quote_documents
from .quotes import build_variation_quote
from .rates import RateLibrary, RateLibraryError, load_rate_library
from .repricing import export_repricing, prepare_repricing
from .schema import ExteriorFinish, HouseSettings

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).parent / "static"
MAX_BODY = 4_194_304
REVISION = re.compile(r"^[0-9a-f]{12}$")
COMPARISON = re.compile(r"^[0-9a-f]{12}_[0-9a-f]{12}_[0-9a-f]{12}$")
REPRICE_RUN = re.compile(r"^[0-9a-f]{24}$")
COMPARISON_CONTRACT = "house-sync-client-quotes-1"
COMPARISON_SOURCES = ("changes.py", "schema.py", "rates.py", "quotes.py", "quote_documents.py", "server.py")


def _quote_issue_date() -> date:
    """Freeze the UTC calendar date when a comparison is first prepared."""
    return datetime.now(UTC).date()


def _comparison_source_fingerprint() -> str:
    source = [COMPARISON_CONTRACT.encode("ascii")]
    for name in COMPARISON_SOURCES:
        source.extend((name.encode("ascii"), Path(__file__).with_name(name).read_bytes()))
    return hashlib.sha256(b"\0".join(source)).hexdigest()


def _current_comparison_contract(comparison: dict) -> bool:
    """A historical report without quote artefacts is left intact on upgrade."""
    if comparison.get("report_contract") != COMPARISON_CONTRACT or "quote" not in comparison:
        return False
    quote_snapshot = comparison["quote"]
    quote_files = {"client_quote_pdf", "internal_breakdown_pdf"}
    if comparison.get("currency") != "AUD":
        return quote_snapshot is None and not quote_files.intersection(comparison.get("files", {}))
    return (isinstance(quote_snapshot, dict) and isinstance(quote_snapshot.get("public"), dict)
            and isinstance(quote_snapshot.get("internal"), dict) and quote_files.issubset(comparison.get("files", {})))


class LocalHTTPServer(ThreadingHTTPServer):
    """Keep one application in exclusive control of its local port on Windows."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def draft_drawing_svg(settings: HouseSettings) -> str:
    """Draw the editable layout without issuing files or running a priced CAD build."""
    layout = derive_layout(settings, validate=True)
    starts = {item["opening_id"]: item["start_mm"] for item in layout["connections"]}
    drawing = {
        "length_mm": settings.length_mm, "width_mm": settings.width_mm,
        "height_mm": settings.height_mm, "wall_thickness_mm": settings.wall_thickness_mm,
        "slab_thickness_mm": SLAB_THICKNESS_MM, "roof_thickness_mm": ROOF_THICKNESS_MM,
        "roof_overhang_mm": ROOF_OVERHANG_MM, "rooms": layout["rooms"], "partitions": layout["partitions"],
        "openings": [{**opening.model_dump(exclude={"offset_mm"}), "start_mm": starts[opening.id]}
                     for opening in settings.openings],
    }
    return drawing_svg({"revision": "DRAFT", "settings": settings.model_dump(mode="json"), "drawing": drawing})


class Application:
    def __init__(self, output_root: Path, rate_library_path: Path | None = None, pricing_path: Path | None = None):
        self.output_root = output_root.resolve()
        self.rate_library_path = rate_library_path
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.latest: dict | None = None
        self.changes_root = self.output_root / "changes"
        self.repricing_root = self.output_root / "repricing"
        self.pricing_path = pricing_path or self.output_root / "pricing.json"
        self.explicit_pricing_path = pricing_path is not None

    def read_revision(self, revision: str) -> dict:
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise ValueError("Choose valid saved revisions A and B.")
        path = self.output_root / revision / "result.json"
        if not path.is_file():
            raise FileNotFoundError(f"Saved revision {revision} was not found.")
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("revision") != revision:
            raise ValueError("The saved revision record does not match its folder.")
        return result

    def _previous(self) -> dict | None:
        if self.latest:
            return self.latest
        pointer = self.output_root / "latest.json"
        if pointer.is_file():
            return self.read_revision(json.loads(pointer.read_text(encoding="utf-8"))["revision"])
        return None

    def pricing_library(self) -> RateLibrary:
        pricing = self.pricing_path if self.explicit_pricing_path or self.pricing_path.exists() else None
        return load_rate_library(self.rate_library_path, pricing_path=pricing)

    def surcharges(self) -> dict:
        with self.lock:
            return {"surcharges": (self.pricing_library().pricing or {}).get("surcharges", [])}

    def add_surcharge(self, rule: dict) -> dict:
        with self.lock:
            pricing = self.pricing_library().pricing or {"rates": {}, "surcharges": []}
            updated = {**pricing, "surcharges": [*pricing["surcharges"], rule]}
            validated = load_rate_library(self.rate_library_path, pricing_data=updated).pricing
            atomic_json(self.pricing_path, validated)
            return {"surcharges": validated["surcharges"]}

    def _order_statuses(self, saved: dict) -> dict:
        path = self.output_root / "purchase-order-statuses" / (saved["revision"] + ".json")
        statuses = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else saved.get("purchase_order_statuses", {})
        return validate_purchase_order_statuses(saved, statuses)

    def order_statuses(self, revision: str) -> dict:
        with self.lock:
            saved = self.read_revision(revision)
            return {"statuses": self._order_statuses(saved),
                    "orders": [{"code": row["code"], "description": row["description"]} for row in saved["boq"]]}

    def set_order_status(self, revision: str, code: str, status: str) -> dict:
        with self.lock:
            saved = self.read_revision(revision)
            if not isinstance(code, str):
                raise ValueError("Choose a saved purchase order code.")
            statuses = validate_purchase_order_statuses(saved, {**self._order_statuses(saved), code: status})
            atomic_json(self.output_root / "purchase-order-statuses" / (revision + ".json"), statuses)
            return self.order_statuses(revision)

    def reprice(self, revision: str, as_at: str, before: str | None = None) -> dict:
        """Publish an independent report and preserve the saved revision and orders."""
        with self.lock:
            saved = self.read_revision(revision)
            statuses = self._order_statuses(saved)
            report, baseline, target = prepare_repricing(saved, self.pricing_library(), as_at, before, statuses)
            run_id = report["run_id"]
            destination = self.repricing_root / run_id
            record = destination / "report.json"
            if record.is_file():
                return json.loads(record.read_text(encoding="utf-8"))
            self.repricing_root.mkdir(exist_ok=True)
            temporary = self.repricing_root / ("building-" + uuid.uuid4().hex)
            temporary.mkdir()
            try:
                files = export_repricing(report, baseline, target, saved, statuses, temporary)
                report["files"] = {key: f"/repricing/{run_id}/{quote(name)}" for key, name in files.items()}
                atomic_json(temporary / "report.json", report)
                try:
                    temporary.rename(destination)
                except FileExistsError:
                    # A second CLI/server process can publish the same report first.
                    if record.is_file():
                        return json.loads(record.read_text(encoding="utf-8"))
                    raise
                return report
            finally:
                if temporary.exists() and temporary.resolve().parent == self.repricing_root:
                    shutil.rmtree(temporary)

    def compare(self, before_revision: str, after_revision: str) -> dict:
        """Compare immutable saved inputs; persist a source-versioned report."""
        with self.lock:
            before = self.read_revision(before_revision)
            after = self.read_revision(after_revision)
            fingerprint = _comparison_source_fingerprint()
            version, migration = fingerprint[:12], 0
            while True:
                comparison_id = f"{before_revision}_{after_revision}_{version}"
                destination = self.changes_root / comparison_id
                record = destination / "comparison.json"
                if not destination.exists():
                    break
                if record.is_file():
                    cached = json.loads(record.read_text(encoding="utf-8"))
                    if _current_comparison_contract(cached):
                        return cached
                # Even an older/incomplete cache at the current key must not
                # be overwritten. Publish a new, deterministic upgrade key.
                migration += 1
                version = hashlib.sha256(f"{fingerprint}\0quote-upgrade-{migration}".encode("ascii")).hexdigest()[:12]
            comparison = compare_revisions(before, after)
            comparison["comparison_id"] = comparison_id
            comparison["created_at"] = datetime.now(UTC).isoformat()
            comparison["report_contract"] = COMPARISON_CONTRACT
            comparison["source_fingerprint"] = fingerprint
            comparison["quote"] = None
            if comparison["currency"] == "AUD":
                comparison["quote"] = build_variation_quote(before, after, comparison, _quote_issue_date())
                comparison["quote_date_basis"] = "UTC calendar date of first preparation"
            self.changes_root.mkdir(parents=True, exist_ok=True)
            temporary = self.changes_root / ("building-" + uuid.uuid4().hex)
            temporary.mkdir()
            try:
                files = export_change_report(comparison, temporary)
                if comparison["quote"] is not None:
                    files.update(export_quote_documents(comparison, temporary))
                comparison["files"] = {
                    key: f"/changes/{comparison_id}/" + quote(name, safe="/") for key, name in files.items()
                }
                atomic_json(temporary / "comparison.json", comparison)
                temporary.rename(destination)
            except Exception:
                if temporary.exists() and temporary.resolve().parent == self.changes_root:
                    shutil.rmtree(temporary)
                raise
            return comparison

    def _record_transition(self, before: dict | None, after: dict) -> None:
        if not before or before["revision"] == after["revision"]:
            return
        transition = {"before_revision": before["revision"], "after_revision": after["revision"]}
        try:
            comparison = self.compare(before["revision"], after["revision"])
            transition["comparison_id"] = comparison["comparison_id"]
        except Exception as exc:
            # A comparison/report issue must not misrepresent a completed house build.
            transition["error"] = f"Change report unavailable: {exc}"
        atomic_json(self.output_root / "last_transition.json", transition)

    def envelope(self, result: dict) -> dict:
        """Attach navigation metadata without changing an issued revision file."""
        enriched = {**result, "previous_revision": None, "last_change": None}
        pointer = self.output_root / "last_transition.json"
        if pointer.is_file():
            transition = json.loads(pointer.read_text(encoding="utf-8"))
            if transition.get("after_revision") == result["revision"]:
                enriched["previous_revision"] = transition["before_revision"]
                if transition.get("error"):
                    enriched["change_error"] = transition["error"]
                else:
                    enriched["last_change"] = self.compare(transition["before_revision"], result["revision"])
        return enriched

    def build(self, settings: HouseSettings, *, rate_library: RateLibrary | None = None) -> dict:
        with self.lock:
            library = rate_library if rate_library is not None else load_rate_library(self.rate_library_path)
            previous = self._previous()
            revision = revision_for(settings, library)
            destination = self.output_root / revision
            if (destination / "result.json").is_file():
                self.latest = json.loads((destination / "result.json").read_text(encoding="utf-8"))
                atomic_json(self.output_root / "latest.json", {"revision": revision})
                self._record_transition(previous, self.latest)
                return self.latest
            temporary = self.output_root / ("building-" + uuid.uuid4().hex)
            temporary.mkdir()
            try:
                result = build_house(settings, temporary, library)
                if result["revision"] != revision or not REVISION.fullmatch(revision):
                    raise ValueError("Build inputs changed during generation. Please retry the update.")
                result["files"].update(export_documents(result, temporary))
                result["files"]["settings_json"] = "settings.json"
                atomic_json(temporary / "settings.json", settings.model_dump(mode="json"))
                result["created_at"] = datetime.now(UTC).isoformat()
                result["files"] = {
                    key: "/files/" + revision + "/" + quote(value.replace("\\", "/"), safe="/")
                    for key, value in result["files"].items()
                    if isinstance(value, str)
                }
                atomic_json(temporary / "result.json", result)
                destination = self.output_root / revision
                if destination.exists():
                    # Identical input revisions keep their original published snapshot.
                    published = json.loads((destination / "result.json").read_text(encoding="utf-8"))
                    if temporary.resolve().parent != self.output_root:
                        raise ValueError("Temporary build is outside the output folder")
                    shutil.rmtree(temporary)
                    result = published
                else:
                    temporary.rename(destination)
                atomic_json(self.output_root / "latest.json", {"revision": revision})
                self.latest = result
                self._record_transition(previous, result)
                return result
            except Exception:
                if temporary.exists() and temporary.parent == self.output_root:
                    shutil.rmtree(temporary)
                raise

    def state(self) -> dict:
        with self.lock:
            if self.latest:
                return self.latest
            saved = self._previous()
            if saved is None:
                library = load_rate_library(self.rate_library_path)
                return self.build(HouseSettings(currency=library.currency, exterior_finish=ExteriorFinish()), rate_library=library)
            # Reopening uses the issued pricing basis. Only an explicit build
            # adopts edited CSV prices. Pre-library snapshots have no such basis.
            if "rate_library" in saved:
                issued = RateLibrary(**saved["rate_library"])
                settings = HouseSettings.model_validate(saved["settings"])
                if revision_for(settings, issued) != saved["revision"]:
                    return self.build(settings, rate_library=issued)
            self.latest = saved
            return saved

    def revisions(self) -> list[dict]:
        records = []
        for directory in self.output_root.iterdir():
            if REVISION.fullmatch(directory.name) and (directory / "result.json").is_file():
                result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                records.append({
                    "revision": result["revision"], "created_at": result.get("created_at"), "settings": result["settings"],
                    "project_name": result["settings"].get("project_name", "House"),
                    "currency": result.get("totals", {}).get("currency", result["settings"].get("currency")),
                    "subtotal": result.get("totals", {}).get("subtotal"),
                })
        return sorted(records, key=lambda item: item.get("created_at") or "", reverse=True)


def handler_for(application: Application) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, data: dict | list, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _allowed_host(self) -> bool:
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return self.headers.get("Host") in expected

        def _file(self, path: Path, root: Path, *, attachment: bool = False) -> None:
            path = path.resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                self._json({"error": "File not found"}, 404)
                return
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix == ".js":
                mime = "text/javascript"
            self._content(path.read_bytes(), mime, path.name if attachment else None)

        def _content(self, content: bytes, mime: str, filename: str | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def _purchase_order(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) != 4 or not REVISION.fullmatch(parts[2]):
                self._json({"error": "Purchase order not found"}, 404)
                return
            revision, filename = parts[2:]
            if filename == "all.zip":
                saved = application.read_revision(revision)
                self._content(purchase_orders_zip(saved), "application/zip", f"purchase-orders-{revision}.zip")
                return
            item = re.fullmatch(r"([A-Z][A-Z0-9_]*)\.pdf", filename)
            if not item:
                self._json({"error": "Purchase order not found"}, 404)
                return
            saved = application.read_revision(revision)
            code = item.group(1)
            try:
                pdf = purchase_order_pdf(saved, code)
            except UnknownPurchaseOrder:
                self._json({"error": "Item not found in the saved bill of quantities"}, 404)
                return
            self._content(pdf, "application/pdf", f"PO-{revision}-{code}.pdf")

        def do_GET(self) -> None:
            if not self._allowed_host():
                self._json({"error": "This application accepts localhost requests only."}, 403)
                return
            path = unquote(urlsplit(self.path).path)
            try:
                if path == "/api/health":
                    self._json({"status": "ok", "application": "House Sync", "features": ["revision_comparison", "client_variation_quotes", "dated_pricing"]})
                elif path == "/api/state":
                    self._json(application.envelope(application.state()))
                elif path == "/api/rates":
                    self._json(load_rate_library(application.rate_library_path).snapshot())
                elif path == "/api/revisions":
                    self._json(application.revisions())
                elif path == "/api/surcharges":
                    self._json(application.surcharges())
                elif path == "/api/purchase-order-statuses":
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    revisions = query.get("revision", [])
                    if len(revisions) != 1:
                        raise ValueError("Choose one saved revision.")
                    self._json(application.order_statuses(revisions[0]))
                elif path.startswith("/purchase-orders/"):
                    self._purchase_order(path)
                elif path.startswith("/repricing/"):
                    parts = path.split("/", 3)
                    if len(parts) != 4 or not REPRICE_RUN.fullmatch(parts[2]):
                        self._json({"error": "Repricing report not found"}, 404)
                        return
                    directory = application.repricing_root / parts[2]
                    self._file(directory / parts[3], directory, attachment=True)
                elif path == "/api/compare":
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if len(query.get("before", [])) != 1 or len(query.get("after", [])) != 1:
                        self._json({"error": "Choose one saved revision A and one saved revision B."}, 422)
                        return
                    self._json(application.compare(query["before"][0], query["after"][0]))
                elif path.startswith("/changes/"):
                    parts = path.split("/", 3)
                    if len(parts) != 4 or not COMPARISON.fullmatch(parts[2]):
                        self._json({"error": "Change report not found"}, 404)
                        return
                    directory = application.changes_root / parts[2]
                    self._file(directory / parts[3], directory, attachment=not path.endswith(".pdf"))
                elif path.startswith("/files/"):
                    parts = path.split("/", 3)
                    if len(parts) != 4 or not REVISION.fullmatch(parts[2]):
                        self._json({"error": "File not found"}, 404)
                        return
                    self._file(application.output_root / parts[2] / parts[3], application.output_root / parts[2],
                               attachment=not path.endswith((".svg", ".pdf")))
                elif path.startswith("/vendor/"):
                    vendor = ROOT / "node_modules" / "three"
                    names = {"three.module.js": vendor / "build" / "three.module.js",
                             "three.core.js": vendor / "build" / "three.core.js",
                             "OrbitControls.js": vendor / "examples" / "jsm" / "controls" / "OrbitControls.js"}
                    target = names.get(path.removeprefix("/vendor/"))
                    if target:
                        self._file(target, vendor)
                    else:
                        self._json({"error": "File not found"}, 404)
                else:
                    self._file(STATIC / ("index.html" if path == "/" else path.lstrip("/")), STATIC)
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 404)
            except ValueError as exc:
                self._json({"error": str(exc)}, 422)
            except Exception as exc:
                traceback.print_exc()
                self._json({"error": f"Unable to load project: {exc}"}, 500)

        def _reject_post(self, message: str, status: int) -> None:
            # Drain bounded request bodies before closing. The expanded settings
            # payload otherwise causes Windows to reset the socket before the
            # client can read a rejection response. Never parse or build it.
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if 0 < length <= MAX_BODY:
                timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(1)
                    self.rfile.read(length)
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(timeout)
            self._json({"error": message}, status)

        def do_POST(self) -> None:
            if not self._allowed_host():
                self._reject_post("This application accepts localhost requests only.", 403)
                return
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                self._reject_post("Origin is not allowed", 403)
                return
            route = urlsplit(self.path).path
            if route not in {"/api/build", "/api/layout", "/api/drawings-preview", "/api/shutdown",
                             "/api/reprice", "/api/surcharges", "/api/purchase-order-statuses"}:
                self._json({"error": "Route not found"}, 404)
                return
            if self.headers.get_content_type() != "application/json":
                self._json({"error": "Send application/json"}, 415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY:
                    self._json({"error": "Invalid request size"}, 413)
                    return
                body = self.rfile.read(length)
                if route in {"/api/reprice", "/api/surcharges", "/api/purchase-order-statuses"}:
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ValueError("Send a JSON object.")
                    if route == "/api/reprice":
                        self._json(application.reprice(payload.get("revision"), payload.get("as_at"), payload.get("before")))
                    elif route == "/api/surcharges":
                        self._json(application.add_surcharge(payload), 201)
                    else:
                        self._json(application.set_order_status(payload.get("revision"), payload.get("code"), payload.get("status")))
                    return
                if route == "/api/layout":
                    draft = json.loads(body)
                    if not isinstance(draft, dict):
                        raise ValueError("Send a house design object for the plan preview.")
                    self._json(derive_layout(draft, validate=False))
                    return
                if route == "/api/shutdown":
                    if json.loads(body) != {"application": "House Sync"}:
                        self._json({"error": "Identify the application to stop."}, 422)
                        return
                    self._json({"status": "stopping", "application": "House Sync"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                settings = HouseSettings.model_validate_json(body)
                if route == "/api/drawings-preview":
                    svg = draft_drawing_svg(settings).encode("utf-8")
                    # The browser can cancel a preview when a newer edit arrives.
                    with suppress(ConnectionError):
                        self._content(svg, "image/svg+xml; charset=utf-8")
                    return
                self._json(application.envelope(application.build(settings)))
            except ValidationError as exc:
                messages = [".".join(map(str, item["loc"])) + ": " + item["msg"] for item in exc.errors()]
                self._json({"error": "; ".join(messages)}, 422)
            except RateLibraryError as exc:
                self._json({"error": str(exc)}, 422)
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 404)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                traceback.print_exc()
                self._json({"error": f"Build failed: {exc}. Your previous revision is still available."}, 500)

    return Handler


def main() -> None:
    if sys.argv[1:2] == ["reprice"]:
        parser = argparse.ArgumentParser(prog="house-sync reprice", description="Reprice an existing saved BOQ without rebuilding the design.")
        parser.add_argument("revision")
        parser.add_argument("--as-at", required=True, help="Target pricing date (YYYY-MM-DD)")
        parser.add_argument("--before", help="Compare BOQ against this date instead of the saved rates (YYYY-MM-DD)")
        parser.add_argument("--output", type=Path, default=ROOT / "out" / "house-sync", help="Existing House Sync revision folder")
        parser.add_argument("--rates", type=Path, help="Path to the project unit-rate CSV")
        parser.add_argument("--pricing", type=Path, help="Path to the dated pricing JSON")
        args = parser.parse_args(sys.argv[2:])
        application = Application(args.output, args.rates, args.pricing)
        try:
            report = application.reprice(args.revision, args.as_at, args.before)
        except (FileNotFoundError, RateLibraryError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps({key: report[key] for key in ("revision", "as_at", "before", "totals", "run_id")}
                         | {"draft_count": len(report["purchase_orders"]["orders"]),
                            "updated_count": report["purchase_orders"]["updated_count"],
                            "variation_count": report["purchase_orders"]["variation_count"],
                            "files": {key: str(application.output_root / value.lstrip("/"))
                                      for key, value in report["files"].items()}}, indent=2))
        return
    parser = argparse.ArgumentParser(description="Run the House Sync editor on this PC.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true", help="Open the editor in the default browser")
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "house-sync")
    parser.add_argument("--rates", type=Path, help="Path to the project unit-rate CSV (default: rates/rates_aud.csv)")
    parser.add_argument("--pricing", type=Path, help="Path to the dated pricing JSON (default: pricing.json in the output folder)")
    args = parser.parse_args()
    if not (ROOT / "node_modules" / "three" / "build" / "three.module.js").exists():
        parser.error("Browser dependencies are missing. Run npm ci in the project folder first.")
    application = Application(args.output, rate_library_path=args.rates, pricing_path=args.pricing)
    server = LocalHTTPServer(("127.0.0.1", args.port), handler_for(application))
    url = f"http://127.0.0.1:{args.port}"
    print(f"House Sync is running at {url}\nRevision files: {args.output}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHouse Sync stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
