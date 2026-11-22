"""Compare and export a saved design at a selected AUD pricing date."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal
from html import escape
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .changes import compare_revisions
from .engine import reprice_boq
from .purchasing import reprice_purchase_orders, repriced_purchase_orders_zip
from .quote_documents import export_quote_documents
from .quotes import build_variation_quote
from .rates import RateLibrary, pricing_date

REPORT_VERSION = 1
AMOUNTS = ("old_amount", "new_amount", "delta")
HEADERS = ("Code", "Description", "Trade", "Supplier", "Quantity", "Unit", "Old rate", "New rate",
           "Old amount", "New amount", "Change", "Causes", "Applied surcharges")
WIDTHS = (22, 42, 22, 22, 13, 8, 13, 13, 14, 14, 14, 42, 36)


def _decimal(value: int | float) -> Decimal:
    return Decimal(str(value))


def _amounts(old: Decimal, new: Decimal) -> dict:
    return dict(zip(AMOUNTS, (float(old), float(new), float(new - old)), strict=True))


def _sum_amounts(rows: list[dict]) -> dict:
    return _amounts(*(sum((_decimal(row[key]) for row in rows), Decimal(0)) for key in AMOUNTS[:2]))


def _group(rows: list[dict], key: str) -> list[dict]:
    groups = defaultdict(list)
    for line in rows:
        groups[line[key]].append(line)
    return [{"name": name, **_sum_amounts(groups[name])} for name in sorted(groups, key=lambda value: (value.casefold(), value))]


def _surcharge_changes(old: list[dict], new: list[dict]) -> list[str]:
    def identity(rule):
        return rule["name"], rule["component"], rule["effective_from"]

    def label(rule):
        return f"{rule['name']} (from {rule['effective_from']})"

    changes = []
    old_by_key = {identity(rule): rule for rule in old}
    new_keys = {identity(rule) for rule in new}
    for rule in new:
        match = old_by_key.get(identity(rule))
        if match is None:
            changes.append(f"{label(rule)} applied to {rule['component']}")
        elif match["unit_rate_change"] != rule["unit_rate_change"]:
            changes.append(f"{label(rule)} changed on {rule['component']}")
    for rule in old:
        if identity(rule) not in new_keys:
            changes.append(f"{label(rule)} removed from {rule['component']}")
    return changes


def _causes(old: dict, new: dict) -> list[str]:
    if old["rate"] == new["rate"]:
        return []
    changes = _surcharge_changes(old.get("surcharges", []), new.get("surcharges", []))
    old_components = old.get("rate_base_components")
    new_components = new.get("rate_base_components")
    if old_components is not None and new_components is not None:
        for component in sorted(old_components.keys() | new_components.keys()):
            before, after = old_components.get(component, 0), new_components.get(component, 0)
            if before != after:
                effective = new.get("rate_component_dates", {}).get(component)
                changes.append(f"{component.capitalize()} rate changed from {before:g} to {after:g}"
                               + (f" (effective {effective})" if effective else ""))
    else:
        new_base = sum((_decimal(value) for value in (new_components or {}).values()), Decimal(0))
        if new_components is not None and new_base != _decimal(old["rate"]):
            dated = new.get("rate_changes", [])
            changes.extend(f"{item['component'].capitalize()} rate changed from {item['old_rate']:g} to {item['new_rate']:g}"
                           f" (effective {item['effective_from']})" for item in dated)
            if not dated:
                changes.append(f"Library base rate changed from {old['rate']:g} to {new_base:g}")
    return changes or ["Effective unit rate changed"]


def _lines(baseline: dict, target: dict) -> list[dict]:
    originals = {row["code"]: row for row in baseline["boq"]}
    lines = []
    for row in target["boq"]:
        old = originals[row["code"]]
        lines.append({
            "code": row["code"], "rate_code": row.get("rate_code", row["code"]), "description": row["description"],
            "quantity": row.get("gross_quantity", row.get("quantity")), "unit": row["unit"], "trade": row.get("trade") or "Unassigned",
            "supplier": row.get("supplier") or "Unassigned", "old_rate": old["rate"], "new_rate": row["rate"],
            **_amounts(_decimal(old["amount"]), _decimal(row["amount"])),
            "causes": _causes(old, row), "applied_surcharges": row.get("surcharges", []),
            "rate_components": row.get("rate_components", {}),
        })
    return lines


def prepare_repricing(saved: dict, library: RateLibrary, as_at: str, before: str | None,
                      statuses: dict) -> tuple[dict, dict, dict]:
    """Build the detached result and its audit report from one immutable BOQ."""
    if saved["totals"]["currency"] != "AUD":
        raise ValueError("Dated pricing requires an AUD revision.")
    target_date = pricing_date(as_at)
    baseline_date = pricing_date(before) if before is not None else None
    target = reprice_boq(saved, library, target_date)
    baseline = reprice_boq(saved, library, baseline_date) if baseline_date else saved
    lines = _lines(baseline, target)
    orders = reprice_purchase_orders(saved, target, statuses)
    report = {
        "report_version": REPORT_VERSION, "revision": saved["revision"], "as_at": target_date,
        "before": baseline_date, "currency": "AUD",
        "rate_library_sha256": library.sha256, "priced_rate_library": target["rate_library"],
        "totals": _sum_amounts(lines), "lines": lines, "by_trade": _group(lines, "trade"),
        "by_supplier": _group(lines, "supplier"), "purchase_orders": orders,
    }
    key = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    report["run_id"] = hashlib.sha256(key).hexdigest()[:24]
    return report, baseline, target


def _display_rows(report: dict) -> list[list]:
    return [[*(row[key] for key in ("code", "description", "trade", "supplier", "quantity", "unit",
                                    "old_rate", "new_rate", *AMOUNTS)),
             "; ".join(row["causes"]),
             "; ".join(f"{rule['name']} ({rule['component']}, AUD {rule['unit_rate_change']:+g}/unit)"
                       for rule in row["applied_surcharges"])] for row in report["lines"]]


def _safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _summary_rows(report, key):
    return [[row["name"], *(row[field] for field in AMOUNTS)] for row in report[key]]


def _write_excel(report, rows, path):
    book = Workbook()
    book.active.title = "BOQ"
    book.properties.creator = "House Sync"
    for sheet, headings, data, widths in (
        (book.active, HEADERS, rows, WIDTHS),
        (book.create_sheet("By trade"), ("Trade", "Old amount", "New amount", "Change"), _summary_rows(report, "by_trade"), (36, 16, 16, 16)),
        (book.create_sheet("By supplier"), ("Supplier", "Old amount", "New amount", "Change"), _summary_rows(report, "by_supplier"), (36, 16, 16, 16)),
    ):
        sheet.append([f"Revision {report['revision']} • {report['before'] or 'Saved BOQ'} to {report['as_at']} • Illustrative AUD, ex GST"])
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headings))
        sheet.append(headings)
        for row in data:
            sheet.append([_safe_cell(value) for value in row])
        sheet.append(["Total", *([None] * (headings.index("Old amount") - 1)), *(report["totals"][key] for key in AMOUNTS)])
        sheet.freeze_panes = "B3"
        sheet.auto_filter.ref = f"A2:{get_column_letter(len(headings))}{sheet.max_row - 1}"
        for column, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(column)].width = width
        for cell in sheet[2]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="173042")
        for cells in sheet.iter_rows(min_row=3):
            for cell in cells:
                if isinstance(cell.value, (int, float)):
                    amount = headings[cell.column - 1] in ("Old amount", "New amount", "Change")
                    cell.number_format = "#,##0.00" if amount else "#,##0.######"
                elif isinstance(cell.value, str):
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
    memory = BytesIO()
    book.save(memory)
    # Openpyxl supplies the wall clock for the core properties and ZIP entries.
    with ZipFile(memory) as source, ZipFile(path, "w", compression=ZIP_DEFLATED) as output:
        for name in source.namelist():
            content = source.read(name)
            if name == "docProps/core.xml":
                content = re.sub(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]+",
                                 lambda match: match[1] + f"{report['as_at']}T00:00:00Z".encode(), content)
            output.writestr(ZipInfo(name), content, compress_type=ZIP_DEFLATED)


def _write_pdf(report, rows, path):
    styles = getSampleStyleSheet()
    text = ParagraphStyle("Cell", parent=styles["BodyText"], fontSize=7, leading=9, spaceAfter=0)
    header = ParagraphStyle("Header", parent=text, textColor=colors.white, fontName="Helvetica-Bold")

    def table(headings, data, widths):
        cells = [[Paragraph(escape(str(value)), header) for value in headings]]
        cells.extend([Paragraph(escape(str(value)), text) for value in row] for row in data)
        result = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT", splitInRow=1)
        result.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173042")),
                                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EAF3F7")]),
                                    ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return result

    old_label = report["before"] or "saved BOQ"
    totals = report["totals"]
    story = [Paragraph("BOQ repricing", styles["Title"]),
             Paragraph(f"Revision {report['revision']} • {old_label} to {report['as_at']} • Illustrative AUD, ex GST", styles["BodyText"]),
             Paragraph(f"{totals['old_amount']:,.2f} to {totals['new_amount']:,.2f} ({totals['delta']:+,.2f})", styles["Heading2"])]
    data = [[*row[:4], f"{row[4]:g} {row[5]}", f"{row[6]:,.6f}".rstrip("0").rstrip("."),
             f"{row[7]:,.6f}".rstrip("0").rstrip("."), f"{row[10]:+,.2f}", "; ".join(filter(None, row[-2:]))] for row in rows]
    story.append(table(("Code", "Description", "Trade", "Supplier", "Quantity", "Old rate", "New rate", "Change", "Causes and applied surcharges"),
                       data, [88, 136, 60, 65, 54, 52, 52, 57, 190]))
    for title, key in (("Totals by trade", "by_trade"), ("Totals by supplier", "by_supplier")):
        story.extend([Spacer(1, 10), Paragraph(title, styles["Heading2"]),
                      table(("Group", "Old amount", "New amount", "Change"),
                            [[row[0], f"{row[1]:,.2f}", f"{row[2]:,.2f}", f"{row[3]:+,.2f}"] for row in _summary_rows(report, key)],
                            [220, 90, 90, 90])])
    SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=32, rightMargin=32, topMargin=28, bottomMargin=28,
                      title="House Sync BOQ repricing", author="House Sync", invariant=True).build(story)


def export_repricing(report: dict, baseline: dict, target: dict, saved: dict, statuses: dict, directory: Path) -> dict[str, str]:
    """Write repeatable downloads, including the existing client quote layout."""
    rows = _display_rows(report)
    files = {"csv": "repricing.csv", "xlsx": "repricing.xlsx", "pdf": "repricing.pdf", "purchase_orders": "purchase-orders.zip"}
    with (directory / files["csv"]).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Revision", report["revision"], "Old pricing", report["before"] or "Saved BOQ", "Price as at", report["as_at"], "Illustrative AUD, ex GST"])
        writer.writerow(HEADERS)
        writer.writerows([_safe_cell(value) for value in row] for row in rows)
        writer.writerow(["Total", *([None] * 7), *(report["totals"][key] for key in AMOUNTS)])
        for name, key in (("Trade", "by_trade"), ("Supplier", "by_supplier")):
            writer.writerow([])
            writer.writerow([name, "Old amount", "New amount", "Change"])
            writer.writerows([_safe_cell(value) for value in row] for row in _summary_rows(report, key))
    _write_excel(report, rows, directory / files["xlsx"])
    _write_pdf(report, rows, directory / files["pdf"])
    (directory / files["purchase_orders"]).write_bytes(repriced_purchase_orders_zip(saved, target, statuses))
    if any(row["delta"] for row in report["lines"]):
        comparison = compare_revisions(baseline, target)
        comparison["quote"] = build_variation_quote(baseline, target, comparison, report["as_at"])
        for section in comparison["quote"].values():
            section["quote_reference"] += "-" + report["run_id"][:8]
        files.update(export_quote_documents(comparison, directory))
    return files
