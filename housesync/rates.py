"""Validated CSV unit rates and optional dated AUD pricing rules.

The repository CSV is the editable source. Wheels carry the same bytes as
package data so an installed application does not depend on its working folder.
Loading never infers a supplier quotation or performs a currency conversion.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

BOQ_CODES: dict[str, tuple[str, str]] = {
    "FLOOR": ("m2", "floor_m2"),
    "WALL": ("m3", "wall_m3"),
    "ROOF": ("m2", "roof_m2"),
    "PAINT": ("m2", "paint_m2"),
    "DOOR": ("each", "opening_each"),
    "WINDOW": ("each", "opening_each"),
    "FLOOR_FINISH": ("m2", "floor_finish_m2"),
    "CEILING": ("m2", "ceiling_m2"),
    "SKIRTING": ("m", "skirting_m"),
    "INTERNAL_PLASTER": ("m2", "internal_plaster_m2"),
    "EXTERNAL_RENDER": ("m2", "external_render_m2"),
}
CSV_COLUMNS = ("code", "description", "unit", "rate", "currency", "source", "effective_date", "notes")
COMPONENTS = ("material", "labour", "plant", "freight")
TRADE_NAMES = {
    "FLOOR": "Concrete", "WALL": "Masonry", "ROOF": "Roofing", "PAINT": "Painting",
    "DOOR": "Doors", "WINDOW": "Windows", "FLOOR_FINISH": "Flooring", "CEILING": "Ceilings",
    "SKIRTING": "Carpentry", "INTERNAL_PLASTER": "Plastering", "EXTERNAL_RENDER": "Rendering",
}
SCOPES = ("library", "trade", "supplier", "material_category", "rate_codes")


def _default_path() -> Path:
    package = Path(__file__).resolve().parent
    packaged = package / "rate_data" / "rates_aud.csv"
    return packaged if packaged.is_file() else package.parent / "rates" / "rates_aud.csv"


DEFAULT_LIBRARY_PATH = _default_path()


class RateLibraryError(ValueError):
    """A rate library cannot be used safely as a complete pricing basis."""


@dataclass(frozen=True)
class RateLibrary:
    currency: str
    sha256: str
    rows: dict[str, dict[str, Any]]
    pricing: dict[str, Any] | None = None
    price_as_at: str | None = None
    base_rows: dict[str, dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return a detached, JSON-safe record for an immutable house revision."""
        snapshot = {"sha256": self.sha256, "currency": self.currency, "rows": deepcopy(self.rows)}
        if self.pricing is not None:
            snapshot["pricing"] = deepcopy(self.pricing)
        if self.price_as_at is not None:
            snapshot.update(price_as_at=self.price_as_at, base_rows=deepcopy(self.base_rows))
        return snapshot

    def as_at(self, value: str | date) -> RateLibrary:
        """Resolve historical component rates and active surcharges for a date."""
        as_at = pricing_date(value)
        if self.price_as_at == as_at:
            return self
        base_rows = self.base_rows if self.base_rows is not None else self.rows
        pricing = self.pricing or {}
        configs = pricing.get("rates", {})
        surcharges = pricing.get("surcharges", [])
        rows = {code: _priced_row(row, configs.get(code, {}), surcharges, as_at) for code, row in base_rows.items()}
        return RateLibrary(self.currency, self.sha256, rows, self.pricing, as_at, base_rows)


def _failure(line: int, message: str) -> RateLibraryError:
    return RateLibraryError(f"Rates CSV line {line}: {message}")


def _text(value: str | None, field: str, line: int, *, required: bool = True) -> str:
    if value is None:
        raise _failure(line, f"missing {field} cell")
    cleaned = value.strip()
    if required and not cleaned:
        raise _failure(line, f"{field} must not be empty")
    if any(ord(character) < 32 for character in cleaned):
        raise _failure(line, f"{field} must be single-line text without control characters")
    return cleaned


def _rate(value: str, line: int) -> float:
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise _failure(line, "rate must be a finite positive number") from exc
    if not decimal.is_finite() or decimal <= 0 or decimal > 1_000_000:
        raise _failure(line, "rate must be finite, positive and no more than 1000000")
    number = float(decimal)
    if not math.isfinite(number) or number <= 0:
        raise _failure(line, "rate must be representable as a positive finite number")
    return number


def pricing_date(value: str | date) -> str:
    """Parse an ISO calendar date without consulting the system clock."""
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise RateLibraryError(f"Pricing date must be a valid YYYY-MM-DD calendar date, got {value!r}")


def _object(value: Any, keys: set[str], label: str, required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise RateLibraryError(f"{label} must be an object")
    unexpected = value.keys() - keys
    missing = required - value.keys()
    if unexpected or missing:
        raise RateLibraryError(f"{label} has missing fields {sorted(missing)} and unexpected fields {sorted(unexpected)}")
    return value


def _items(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise RateLibraryError(f"{label} must be an array")
    return value


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise RateLibraryError(f"{label} must be non-empty, single-line text")
    return value.strip()


def _pricing_number(value: Any, label: str, *, allow_zero: bool = False) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1_000_000 or (not allow_zero and value == 0):
        bound = "zero" if allow_zero else "above zero"
        raise RateLibraryError(f"{label} must be a number from {bound} to 1000000")
    return float(value)


def _components(value: Any, label: str) -> dict[str, float]:
    components = _object(value, set(COMPONENTS), label)
    if not components:
        raise RateLibraryError(f"{label} needs at least one component")
    return {key: _pricing_number(amount, f"{label}.{key}", allow_zero=True) for key, amount in components.items()}


def _decimal_components(values: dict[str, float]) -> dict[str, Decimal]:
    return {component: Decimal(str(value)) for component, value in values.items()}


def _versions(value: Any, code: str) -> list[dict]:
    versions = []
    seen = set()
    for raw in _items(value, f"{code} versions"):
        item = _object(raw, {"effective_from", "rate", "components", "replace"}, f"{code} version", {"effective_from"})
        start = pricing_date(item["effective_from"])
        if start in seen:
            raise RateLibraryError(f"{code} has multiple versions effective {start}; put all component changes for that day in one version")
        seen.add(start)
        if "rate" in item and "components" not in item and "replace" not in item:
            versions.append({"effective_from": start, "rate": _pricing_number(item["rate"], f"{code} {start} rate")})
        elif "components" in item and "rate" not in item and type(item.get("replace", False)) is bool:
            versions.append({"effective_from": start, "components": _components(item["components"], f"{code} {start} components"),
                             "replace": item.get("replace", False)})
        else:
            raise RateLibraryError(f"{code} {start} needs either a rate or components with an optional boolean replace")
    return sorted(versions, key=lambda item: item["effective_from"])


def _surcharges(value: Any) -> list[dict]:
    surcharges = []
    seen = set()
    required = {"name", "effective_from", "type", "amount", "component", "scope"}
    for raw in _items(value, "Surcharges"):
        item = _object(raw, required | {"end_date"}, "Surcharge", required)
        name = _name(item["name"], "Surcharge name")
        start = pricing_date(item["effective_from"])
        end = pricing_date(item["end_date"]) if item.get("end_date") is not None else None
        if end is not None and end < start:
            raise RateLibraryError(f"{name}: end date is before effective-from date")
        identity = (name.casefold(), start)
        if identity in seen:
            raise RateLibraryError(f"Duplicate surcharge {name!r} effective {start}")
        seen.add(identity)
        kind, component = item["type"], item["component"]
        if kind not in ("percentage", "fixed") or component not in ("base", *COMPONENTS):
            raise RateLibraryError(f"{name}: use percentage or fixed and select a valid component")
        scope = _object(item["scope"], {"type", "values"}, f"{name} scope", {"type"})
        scope_type = scope["type"]
        if scope_type not in SCOPES:
            raise RateLibraryError(f"{name}: unknown surcharge scope {scope_type!r}")
        values = [_name(v, f"{name} scope value") for v in _items(scope.get("values", []), f"{name} scope values")]
        if (scope_type == "library") != (not values):
            raise RateLibraryError(f"{name}: scopes other than library need one or more values, and library needs none")
        if scope_type == "rate_codes" and any(code.upper() not in BOQ_CODES for code in values):
            raise RateLibraryError(f"{name}: scope includes an unknown rate code")
        surcharges.append({"name": name, "effective_from": start, "end_date": end, "type": kind,
                           "amount": _pricing_number(item["amount"], f"{name} amount"),
                           "component": component, "scope": {"type": scope_type, "values": values}})
    return surcharges


def _rate_schedule(row: dict, config: dict):
    """Yield the baseline and every full rate state in chronological order."""
    components = _decimal_components(config.get("components", {"base": row["rate"]}))
    yield None, components
    for item in config.get("versions", []):
        start = item["effective_from"]
        if "rate" in item:
            components = {"base": Decimal(str(item["rate"]))}
        else:
            if "base" in components and not item["replace"]:
                raise RateLibraryError(f"{row['code']} {start}: use replace=true when replacing a whole rate with components")
            components = {**({} if item["replace"] else components), **_decimal_components(item["components"])}
        if not 0 < sum(components.values()) <= 1_000_000:
            raise RateLibraryError(f"{row['code']} {start}: component total must be positive and no more than 1000000")
        yield start, components


def _pricing_config(value: Any, rows: dict) -> dict:
    config = _object(value, {"rates", "surcharges"}, "Pricing configuration")
    schedules = _object(config.get("rates", {}), set(rows), "Pricing rates")
    normalized = {}
    for code, raw in schedules.items():
        metadata = {"trade", "supplier", "material_category"}
        schedule = _object(raw, metadata | {"components", "versions"}, f"{code} pricing")
        item = {key: _name(schedule[key], f"{code} {key}") for key in sorted(metadata & schedule.keys())}
        if "components" in schedule:
            item["components"] = _components(schedule["components"], f"{code} baseline components")
            if sum(_decimal_components(item["components"]).values()) != Decimal(str(rows[code]["rate"])):
                raise RateLibraryError(f"{code} baseline components must add up to its CSV rate")
        item["versions"] = _versions(schedule.get("versions", []), code)
        # Validate all scheduled states, including versions past the selected date.
        for _ in _rate_schedule(rows[code], item):
            pass
        normalized[code] = item
    return {"rates": normalized, "surcharges": _surcharges(config.get("surcharges", []))}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RateLibraryError(f"Duplicate pricing JSON key {key!r}")
        result[key] = value
    return result


def _priced_row(row: dict, config: dict, rules: list[dict], as_at: str) -> dict:
    code = row["code"]
    metadata = {"trade": config.get("trade", TRADE_NAMES[code]), "supplier": config.get("supplier", "Unassigned"),
                "material_category": config.get("material_category", "Unassigned")}
    changes = []
    component_dates = {}
    components = {}
    effective_date = row["effective_date"]
    for start, updated in _rate_schedule(row, config):
        if start is None:
            component_dates = dict.fromkeys(updated, effective_date)
        elif start <= as_at:
            effective_date = start
            for key in sorted(updated.keys() | components.keys()):
                before = components.get(key)
                after = updated.get(key)
                if before == after:
                    continue
                changes.append({"component": key, "effective_from": start, "old_rate": float(before or 0), "new_rate": float(after or 0)})
                if after is None:
                    component_dates.pop(key)
                else:
                    component_dates[key] = start
        else:
            break
        components = updated
    base_components = {key: float(value) for key, value in components.items()}
    components = components.copy()
    applied = []
    for rule in rules:
        key = rule["component"]
        if key not in components or rule["effective_from"] > as_at or (rule["end_date"] and rule["end_date"] < as_at):
            continue
        scope = rule["scope"]
        scope_type = scope["type"]
        scope_value = code if scope_type == "rate_codes" else metadata.get(scope_type, "")
        if scope_type != "library" and scope_value.casefold() not in {value.casefold() for value in scope["values"]}:
            continue
        amount = Decimal(str(rule["amount"]))
        delta = components[key] * amount / 100 if rule["type"] == "percentage" else amount
        components[key] += delta
        applied.append({**deepcopy(rule), "unit_rate_change": float(delta)})
        if components[key] > 1_000_000_000_000:
            raise RateLibraryError(f"{code}: stacked surcharges exceed the supported unit rate")
    return {**row, **metadata, "rate": float(sum(components.values())), "effective_date": effective_date,
            "rate_base_components": base_components, "rate_components": {key: float(value) for key, value in components.items()},
            "rate_component_dates": component_dates, "surcharges": applied, "rate_changes": changes}


def load_rate_library(
    path: str | Path | None = None,
    *,
    pricing_path: str | Path | None = None,
    pricing_data: dict | None = None,
) -> RateLibrary:
    """Load exactly one valid rate for every known BOQ code.

    Custom libraries may use any single, uppercase three-letter currency. The
    caller must check its design currency; this loader never converts prices.
    The SHA-256 identifies raw CSV bytes, including metadata and line endings,
    and the canonical validated pricing configuration when supplied.
    Invalid rows fail explicitly with their physical CSV line number.
    """
    raw = Path(path if path is not None else DEFAULT_LIBRARY_PATH).read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise _failure(1, "file must be UTF-8 text") from exc
    reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
    try:
        headers = reader.fieldnames
        if headers is None:
            raise _failure(1, "missing header")
        if len(headers) != len(set(headers)):
            raise _failure(1, "duplicate header columns")
        if set(headers) != set(CSV_COLUMNS):
            missing = sorted(set(CSV_COLUMNS) - set(headers))
            extra = sorted(set(headers) - set(CSV_COLUMNS))
            raise _failure(1, f"header columns must be {', '.join(CSV_COLUMNS)}; missing {missing}; unexpected {extra}")
        rows: dict[str, dict[str, Any]] = {}
        currency: str | None = None
        for source_row in reader:
            line = reader.line_num
            if None in source_row:
                raise _failure(line, "unexpected extra cells")
            row = {field: _text(source_row[field], field, line, required=field != "notes") for field in CSV_COLUMNS}
            code = row["code"]
            if code not in BOQ_CODES:
                raise _failure(line, f"unknown BOQ code {code!r}")
            if code in rows:
                raise _failure(line, f"duplicate BOQ code {code}")
            expected_unit = BOQ_CODES[code][0]
            if row["unit"] != expected_unit:
                raise _failure(line, f"{code} unit must be {expected_unit!r}, got {row['unit']!r}")
            if not re.fullmatch(r"[A-Z]{3}", row["currency"]):
                raise _failure(line, "currency must be an uppercase three-letter code")
            if currency is not None and currency != row["currency"]:
                raise _failure(line, f"mixed currencies: expected {currency}, got {row['currency']}")
            currency = row["currency"]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["effective_date"]):
                raise _failure(line, "effective_date must use YYYY-MM-DD")
            try:
                date.fromisoformat(row["effective_date"])
            except ValueError as exc:
                raise _failure(line, "effective_date must be a valid calendar date") from exc
            rows[code] = {**row, "rate": _rate(row["rate"], line)}
        missing_codes = sorted(BOQ_CODES.keys() - rows.keys())
        if missing_codes:
            raise _failure(max(reader.line_num, 1), f"missing BOQ codes: {', '.join(missing_codes)}")
    except csv.Error as exc:
        # DictReader updates its own line_num only after a successful row. The
        # underlying reader still records the failing physical line on errors.
        line = max(reader.reader.line_num, reader.line_num, 1)
        raise _failure(line, f"invalid CSV: {exc}") from exc
    pricing = None
    digest = hashlib.sha256(raw)
    if pricing_path is not None and pricing_data is not None:
        raise RateLibraryError("Provide either a pricing file or pricing data")
    if pricing_path is not None or pricing_data is not None:
        if currency != "AUD":
            raise RateLibraryError("Dated pricing configuration requires an AUD rate library")
        if pricing_path is not None:
            try:
                pricing_data = json.loads(Path(pricing_path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RateLibraryError(f"Pricing file must be valid UTF-8 JSON: {exc}") from exc
        pricing = _pricing_config(pricing_data, rows)
        digest.update(b"\0")
        digest.update(json.dumps(pricing, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return RateLibrary(currency=currency or "", sha256=digest.hexdigest(), rows=rows, pricing=pricing)
