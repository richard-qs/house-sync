"""Fillable draft purchase orders and variations from saved and repriced BOQs.

Purchase orders retain the saved BOQ grouping, quantities and measurement basis.
Repricing receives a separate priced snapshot and never edits either input.
"""
from __future__ import annotations

import html
import io
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.acroform import AcroForm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#173042")
TEAL = colors.HexColor("#1B6D65")
PALE = colors.HexColor("#EAF3F7")
GREY = colors.HexColor("#586973")
LINE = colors.HexColor("#C9D9DE")
CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
ORDER_STATUSES = frozenset({"draft", "issued", "accepted"})
CENT = Decimal("0.01")


class UnknownPurchaseOrder(KeyError):
    """A valid BOQ code is absent from the requested saved revision."""


def _code(value: Any) -> str:
    if not isinstance(value, str) or CODE.fullmatch(value) is None:
        raise ValueError("Purchase order codes must use uppercase letters, numbers and underscores, starting with a letter (maximum 80 characters)")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Saved {name} must be a finite non-negative number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Saved {name} must be a finite non-negative number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"Saved {name} must be a finite non-negative number")
    return number


def _number(value: Any, name: str, *, money: bool = False) -> str:
    number = _decimal(value, name)
    # Formatting adds no quantisation: a saved 11.125 rate remains 11.125.
    decimals = max(2 if money else 0, -number.as_tuple().exponent)
    text = format(number, f",.{decimals}f")
    return text if money or "." not in text else text.rstrip("0").rstrip(".")


def _saved_rows(result: dict) -> tuple[str, dict[str, Any], str, dict[str, dict]]:
    if not isinstance(result, dict):
        raise ValueError("A saved house result is required")
    revision = result.get("revision")
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise ValueError("Saved revision identity is missing or unsafe for purchase order filenames")
    settings = result.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("Saved settings must be an object")
    totals = result.get("totals", {})
    if not isinstance(totals, dict):
        raise ValueError("Saved totals must be an object")
    currency = totals.get("currency", settings.get("currency"))
    if currency is not None and (not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None):
        raise ValueError("Saved currency must be an uppercase three-letter code")
    rows = result.get("boq")
    if not isinstance(rows, list):
        raise ValueError("Saved BOQ rows are required")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Saved BOQ rows must be objects")
        code = _code(row.get("code"))
        if code in indexed:
            raise ValueError(f"Saved BOQ has duplicate code {code}")
        indexed[code] = row
    return revision, settings, currency or "Not recorded", indexed


def validate_purchase_order_statuses(result: dict, statuses: dict) -> dict[str, str]:
    """Validate statuses keyed by the saved order's individual BOQ row code."""
    _, _, _, rows = _saved_rows(result)
    if not isinstance(statuses, dict):
        raise ValueError("Purchase order statuses must be an object keyed by saved BOQ code")
    for code, status in statuses.items():
        if code not in rows:
            raise ValueError(f"Unknown saved BOQ code {code!r} in purchase order statuses")
        if not isinstance(status, str) or status not in ORDER_STATUSES:
            raise ValueError(f"Purchase order status for {code} must be draft, issued or accepted")
    return dict(statuses)


def _amount_difference(old: Decimal, new: Decimal) -> dict[str, float]:
    return {"old_amount": float(old), "new_amount": float(new), "difference": float(new - old)}


def reprice_purchase_orders(saved: dict, repriced: dict, order_statuses: dict | None = None) -> dict:
    """Prepare replacement drafts and proposed variations against the saved prices."""
    revision, _, currency, before = _saved_rows(saved)
    after_revision, _, after_currency, after = _saved_rows(repriced)
    if revision != after_revision or before.keys() != after.keys() or currency != "AUD" or after_currency != currency:
        raise ValueError("Purchase order repricing requires the same saved AUD revision and BOQ lines")
    as_at = repriced.get("price_as_at")
    try:
        if not isinstance(as_at, str) or date.fromisoformat(as_at).isoformat() != as_at:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Purchase order repricing requires a price-as-at date in YYYY-MM-DD format") from exc
    statuses = validate_purchase_order_statuses(saved, saved.get("purchase_order_statuses", {}) if order_statuses is None else order_statuses)

    orders, variations = [], []
    for code, old in before.items():
        new = after[code]
        gross = _decimal(old.get("gross_quantity", old.get("quantity")), "gross quantity")
        new_gross = _decimal(new.get("gross_quantity", new.get("quantity")), "repriced gross quantity")
        if gross != new_gross or old.get("unit") != new.get("unit") or old.get("group_id") != new.get("group_id"):
            raise ValueError(f"Purchase order repricing changed the saved basis of {code}")
        old_rate = _decimal(old.get("rate"), "unit rate")
        new_rate = _decimal(new.get("rate"), "repriced unit rate")
        old_amount = _decimal(old.get("amount"), "line amount")
        new_amount = _decimal(new.get("amount"), "repriced line amount")
        old_gst = (old_amount * Decimal("0.10")).quantize(CENT, rounding=ROUND_HALF_UP)
        new_gst = (new_amount * Decimal("0.10")).quantize(CENT, rounding=ROUND_HALF_UP)
        status = statuses.get(code, "draft")
        variation = status != "draft"
        if variation and old_amount == new_amount:
            continue
        original_reference = f"PO-{revision}-{code}"
        order = {
            "code": code,
            "group_id": old.get("group_id"),
            "group_name": old.get("group_name"),
            "status": "draft",
            "original_status": status,
            "original_reference": original_reference,
            "reference": f"{original_reference}-VAR-{as_at}" if variation else original_reference,
            "document_type": "variation" if variation else "purchase_order",
            "lines": [{
                "code": code,
                "description": old.get("description"),
                "quantity": float(gross),
                "unit": old.get("unit"),
                "old_rate": float(old_rate),
                "new_rate": float(new_rate),
                **_amount_difference(old_amount, new_amount),
                "surcharges": new.get("surcharges", []),
                "rate_changes": new.get("rate_changes", []),
            }],
            "totals": {
                "subtotal_ex_gst": _amount_difference(old_amount, new_amount),
                "gst_amount": _amount_difference(old_gst, new_gst),
                "total_incl_gst": _amount_difference(old_amount + old_gst, new_amount + new_gst),
            },
        }
        (variations if variation else orders).append(order)

    return {
        "revision": revision,
        "price_as_at": as_at,
        "currency": currency,
        "gst_percent": 10,
        "orders": orders,
        "variations": variations,
        "updated_count": sum(order["totals"]["subtotal_ex_gst"]["difference"] != 0 for order in orders),
        "variation_count": len(variations),
    }


class _OrderFields(Flowable):
    """A single-page block of editable buyer-supplied commercial particulars."""

    def __init__(self, width: float):
        super().__init__()
        self.width, self.height = width, 380.0

    def draw(self) -> None:
        pdf: Canvas = self.canv
        form: AcroForm = pdf.acroForm
        pdf.setStrokeColor(LINE)
        pdf.line(0, self.height, self.width, self.height)
        pdf.setFillColor(TEAL)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(0, self.height - 17, "Order particulars - complete before issue")
        y = self.height - 36

        def row(fields: list[tuple[str, str, bool]], height: float) -> None:
            nonlocal y
            gutter = 14
            width = (self.width - gutter * (len(fields) - 1)) / len(fields)
            for index, (name, label, multiline) in enumerate(fields):
                x = index * (width + gutter)
                pdf.setFillColor(INK)
                pdf.setFont("Helvetica", 8)
                pdf.drawString(x, y, label)
                form.textfield(name=name, tooltip=label, x=x, y=y - height - 5, width=width, height=height,
                               value="", relative=True, fontName="Helvetica", fontSize=9, textColor=colors.black,
                               borderColor=LINE, fillColor=colors.white, borderWidth=.6, borderStyle="solid",
                               fieldFlags="multiline" if multiline else "", maxlen=3000 if multiline else 300,
                               forceBorder=True)
            y -= height + 20

        row([("buyer", "Buyer / company / contact", True), ("supplier", "Supplier / company / contact", True)], 30)
        row([("delivery_site", "Delivery site / address", True)], 34)
        row([("order_date", "Order date", False), ("required_by", "Required-by date", False)], 20)
        row([("quote_reference", "Supplier quote / reference", False)], 20)
        row([("notes", "Product / SKU, terms or delivery instructions", True)], 42)
        row([("prepared_by", "Prepared by", False), ("approved_by", "Approved by", False)], 20)
        pdf.setFillColor(GREY)
        pdf.setFont("Helvetica", 7.5)
        pdf.drawString(0, 22, "Complete the product / SKU, delivery details and agreed terms; confirm the saved BOQ basis before issue.")
        pdf.drawString(0, 11, "Typed names are not digital signatures. This draft records no approval and does not send an order.")


def purchase_order_pdf(result: dict, code: str, *, repricing: dict | None = None) -> bytes:
    """Return one prefilled, fillable draft PDF for a saved BOQ item.

    A missing valid code raises UnknownPurchaseOrder. Invalid snapshot data or
    unsafe code/revision filenames raise ValueError. Optional historical rate
    and measurement metadata remain unknown rather than receiving new defaults.
    """
    code = _code(code)
    revision, settings, currency, rows = _saved_rows(result)
    if code not in rows:
        raise UnknownPurchaseOrder(code)
    row = rows[code]
    unit = row.get("unit")
    description = row.get("description")
    if not isinstance(unit, str) or not unit.strip() or not isinstance(description, str):
        raise ValueError(f"Saved BOQ {code} needs a description and measurement unit")
    gross = _number(row.get("gross_quantity", row.get("quantity")), "gross quantity")
    rate = _number(row.get("rate"), "unit rate", money=True)
    amount = _number(row.get("amount"), "line amount", money=True)
    reference = repricing["reference"] if repricing else f"PO-{revision}-{code}"
    variation = repricing is not None and repricing["document_type"] == "variation"
    source = row.get("rate_source", settings.get("rate_source"))
    effective = row.get("rate_effective_date", settings.get("rate_date"))
    origin = row.get("rate_origin")
    library = result.get("rate_library") or {}
    if not isinstance(library, dict):
        raise ValueError("Saved rate library metadata must be an object")

    styles = {
        "title": ParagraphStyle("POTitle", fontName="Helvetica-Bold", fontSize=21, leading=25, textColor=INK, spaceAfter=5),
        "heading": ParagraphStyle("POHeading", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=TEAL, spaceBefore=6, spaceAfter=4),
        "body": ParagraphStyle("POBody", fontName="Helvetica", fontSize=9, leading=12, textColor=INK, spaceAfter=4),
        "small": ParagraphStyle("POSmall", fontName="Helvetica", fontSize=7.5, leading=10, textColor=GREY, spaceAfter=4),
        "header": ParagraphStyle("POHeader", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white),
    }

    def paragraph(value: Any, style: str = "body") -> Paragraph:
        return Paragraph(html.escape(str(value)), styles[style])

    if result.get("partitions") and row.get("group_name"):
        description = f"{row['group_name']} — {description}"
    width = A4[0] - 72
    subject = "DRAFT PURCHASE ORDER VARIATION" if variation else "DRAFT PURCHASE ORDER"
    story: list[Any] = [paragraph(subject, "title"),
                        paragraph("Saved BOQ reference - pending commercial completion and approval", "small"),
                        paragraph(f"Project: {settings.get('project_name') or 'Not recorded'}"),
                        paragraph(f"Reference: {reference} | Saved design revision: {revision}", "small"),
                        paragraph(f"{code} - scope and {'repriced costs' if repricing else 'saved price'}", "heading"),
                        paragraph(description)]
    if repricing:
        story.append(paragraph(f"Price as at {result['price_as_at']} | Saved gross quantity: {gross} {unit}", "small"))
        if variation:
            story.append(paragraph(f"Original order: {repricing['original_reference']} ({repricing['original_status']}). "
                                   "Proposed change only. Original order remains in effect.", "small"))
        headings = [currency, "Old", "New", "Difference"]
        line = repricing["lines"][0]
        old_rate = _number(line["old_rate"], "previous rate", money=True)
        rows_data = [("Unit rate", old_rate, rate, "")]
        for label, totals in (("Line amount ex GST", line), ("PO subtotal ex GST", repricing["totals"]["subtotal_ex_gst"]),
                              ("GST (10%)", repricing["totals"]["gst_amount"]), ("PO total incl GST", repricing["totals"]["total_incl_gst"])):
            rows_data.append((label, *(f"{totals[key]:,.2f}" for key in ("old_amount", "new_amount", "difference"))))
        widths = [.31, .23, .23, .23]
    else:
        headings = ["Saved gross quantity", "Unit", f"Saved unit rate ({currency})", f"Saved line amount ({currency})"]
        rows_data = [(gross, unit, rate, amount)]
        widths = [.24, .12, .31, .33]
    table = Table([[paragraph(value, "header") for value in headings],
                   *[[paragraph(value) for value in values] for values in rows_data]],
                  colWidths=[width * fraction for fraction in widths], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), INK), ("BACKGROUND", (0, 1), (-1, -1), PALE),
                              ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
    story.extend([table, Spacer(1, 6)])
    measured = []
    for key, label in (("measured_quantity", "Measured net geometry"), ("net_quantity", "Net priced quantity"),
                       ("wastage_percent", "Wastage"), ("paint_coats", "Paint coats")):
        if row.get(key) is not None:
            suffix = "%" if key == "wastage_percent" else f" {unit}" if key in {"measured_quantity", "net_quantity"} else ""
            measured.append(f"{label}: {_number(row[key], label)}{suffix}")
    if measured:
        story.append(paragraph(" | ".join(measured), "small"))
    if row.get("rate_code", code) == "PAINT":
        note = ("Painting retains the saved m2-per-coat measurement basis and recorded coat count. No conversion to litres, cans or product coverage is made."
                if row.get("paint_coats") is not None else
                f"Painting uses the saved {unit} quantity. A coat count was not recorded for this item; no coat multiplier or volume conversion is inferred.")
        story.append(paragraph(note, "small"))
    story.append(paragraph(f"Rate origin: {origin or 'Not recorded'} | Rate effective date: {effective or 'Not recorded'}", "small"))
    story.append(paragraph(f"Applied rate source: {source or 'Not recorded'}", "small"))
    if library.get("sha256"):
        story.append(paragraph(f"Saved rate library SHA-256: {library['sha256']}", "small"))
    if source and str(source).casefold().startswith("illustrative"):
        story.append(paragraph(f"Illustrative {currency} rates, not a tender estimate or a supplier quotation.", "small"))
    if repricing:
        line = repricing["lines"][0]
        names = [surcharge["name"] for surcharge in line["surcharges"]]
        if names:
            story.append(paragraph("Applied surcharges: " + ", ".join(names), "small"))
        story.append(paragraph("GST shown on illustrative AUD prices. Confirm tax, freight and commercial terms before issue.", "small"))
    else:
        story.append(paragraph("Quantities and prices are copied from the saved revision without re-pricing. Tax, freight and commercial terms require agreement.", "small"))
    story.extend([Spacer(1, 7), _OrderFields(width)])

    def footer(pdf: Canvas, document: Any) -> None:
        pdf.saveState()
        pdf.setStrokeColor(LINE)
        pdf.line(36, 28, A4[0] - 36, 28)
        pdf.setFillColor(GREY)
        pdf.setFont("Helvetica", 7)
        pdf.drawString(36, 17, f"{reference} | DRAFT - pending approval")
        pdf.drawRightString(A4[0] - 36, 17, f"Page {document.page}")
        pdf.restoreState()

    output = io.BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=28, bottomMargin=38,
                                 title=f"Draft purchase order {reference}", author="House Sync", invariant=True, pageCompression=0)
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def purchase_orders_zip(result: dict) -> bytes:
    """Return one deterministic named PDF per saved BOQ row, without file writes."""
    revision, _settings, _currency, rows = _saved_rows(result)
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for code in rows:
            entry = ZipInfo(f"PO-{revision}-{code}.pdf", date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.create_system = 0
            archive.writestr(entry, purchase_order_pdf(result, code))
    return output.getvalue()


def repriced_purchase_orders_zip(saved: dict, repriced: dict, order_statuses: dict | None = None) -> bytes:
    """Return repriced drafts and separate variation PDFs in the existing BOQ grouping."""
    package = reprice_purchase_orders(saved, repriced, order_statuses)
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for order in (*package["orders"], *package["variations"]):
            entry = ZipInfo(f"{order['reference']}.pdf", date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.create_system = 0
            archive.writestr(entry, purchase_order_pdf(repriced, order["code"], repricing=order))
    return output.getvalue()
