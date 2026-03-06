# report_generator.py
# Generates clean SRM damage assessment reports from assessments.db

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import List, Dict, Optional, Any
from datetime import datetime
import html
import base64

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    Image,
)
from reportlab.lib.enums import TA_CENTER


ROOT = Path(__file__).resolve().parent
DEFAULT_LOGO = ROOT / "assets" / "royal_jordanian_logo.png"


def fetch_damage_rows(db_path: str | Path, limit: Optional[int] = 25) -> List[Dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        q = """
        SELECT id, created_utc, aircraft_type, structure, structure_zone,
               side, sta, wl, stringer, frame,
               damage_type, dent_diameter_mm, dent_depth_mm,
               input_text, structured_json, srm_hits_json, result_json
        FROM assessments
        ORDER BY id DESC
        LIMIT ?
        """
        rows = con.execute(q, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _fmt_date(value: Any) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return str(value)


def _location_text(r: Dict[str, Any]) -> str:
    bits: List[str] = []

    side = r.get("side")
    structure = r.get("structure")
    zone = r.get("structure_zone")
    frame = r.get("frame")
    stringer = r.get("stringer")
    sta = r.get("sta")

    if side and side != "ANY":
        bits.append(str(side))
    if structure:
        bits.append(str(structure).replace("_", " "))
    if zone:
        bits.append(str(zone).replace("_", " "))
    if frame not in (None, ""):
        bits.append(f"(FR{frame})")
    if stringer:
        bits.append(f"(STGR #{stringer})")
    if sta not in (None, ""):
        try:
            bits.append(f"STA {float(sta):g}")
        except Exception:
            bits.append(f"STA {sta}")

    return " ".join(bits) if bits else "-"


def _damage_type_text(r: Dict[str, Any]) -> str:
    damage_type = str(r.get("damage_type") or "DAMAGE").replace("_", " ")
    extras: List[str] = []

    if r.get("dent_diameter_mm") not in (None, ""):
        extras.append(f"Dia {float(r['dent_diameter_mm']):g} mm")
    if r.get("dent_depth_mm") not in (None, ""):
        extras.append(f"Depth {float(r['dent_depth_mm']):g} mm")

    if extras:
        return f"{damage_type} ({', '.join(extras)})"
    return damage_type


def _reference_text(r: Dict[str, Any]) -> str:
    try:
        import json

        srm_hits_json = r.get("srm_hits_json")
        if srm_hits_json:
            hits = json.loads(srm_hits_json)
            if isinstance(hits, list) and hits:
                h = hits[0]
                if isinstance(h, dict):
                    title = h.get("doc_title") or h.get("file_name") or "SRM Reference"
                    page = h.get("printed_page") or h.get("pdf_page") or h.get("page")
                    if page:
                        return f"{title} / Page {page}"
                    return str(title)
    except Exception:
        pass

    return "SRM Assessment Tool"


def _remarks_text(r: Dict[str, Any]) -> str:
    parts: List[str] = []

    raw = (r.get("input_text") or "").strip()
    if raw:
        parts.append("Auto-generated")

    try:
        import json

        result_json = r.get("result_json")
        if result_json:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                result_text = parsed.get("result")
                if result_text:
                    parts.append(str(result_text)[:120])
    except Exception:
        pass

    return " • ".join(parts) if parts else "-"


def _status_text(r: Dict[str, Any]) -> str:
    return "Closed"


def _logo_data_uri(logo_path: Path) -> str:
    mime = "image/png"
    ext = logo_path.suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        mime = "image/jpeg"
    elif ext == ".webp":
        mime = "image/webp"

    b64 = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def render_report_html(
    rows: List[Dict[str, Any]],
    *,
    ac_reg: str,
    ac_type: str,
    rev: str,
    msn: str,
    rj_ref: str,
    report_date: Optional[str] = None,
    brand_name: str = "Royal Jordanian",
    logo_path: Optional[str | Path] = None,
) -> str:
    if not report_date:
        report_date = datetime.utcnow().strftime("%d.%m.%Y")

    logo_file = Path(logo_path) if logo_path else DEFAULT_LOGO
    logo_html = ""
    if logo_file.exists():
        logo_html = f'<img src="{_logo_data_uri(logo_file)}" style="max-width:180px; max-height:95px; object-fit:contain;" />'
    else:
        logo_html = f"<div class='logo-fallback'>{html.escape(brand_name)}</div>"

    body_rows = ""
    for i, r in enumerate(rows, start=1):
        body_rows += f"""
        <tr>
            <td class="center">{i}</td>
            <td class="center">{html.escape(str(r.get('id','')))}</td>
            <td>{html.escape(_location_text(r))}</td>
            <td class="center">{html.escape(_damage_type_text(r))}</td>
            <td class="center">{html.escape(_reference_text(r))}</td>
            <td class="center">{html.escape(_fmt_date(r.get('created_utc')))}</td>
            <td class="center">{html.escape(_remarks_text(r))}</td>
            <td class="center">{html.escape(_status_text(r))}</td>
        </tr>
        """

    return f"""
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    font-family: Arial, Helvetica, sans-serif;
    margin: 0;
    padding: 18px;
    background: #f3f3f3;
}}

.page {{
    background: white;
    padding: 26px 28px 32px;
    max-width: 1280px;
    margin: 0 auto;
}}

.header-wrap {{
    display: grid;
    grid-template-columns: 1fr 220px;
    gap: 18px;
    align-items: start;
    margin-bottom: 10px;
}}

.header-table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 13px;
}}

.header-table td {{
    border: 1px solid #444;
    padding: 4px 8px;
}}

.header-label {{
    font-weight: bold;
    width: 14%;
    white-space: nowrap;
}}

.header-value {{
    color: #2f5d9a;
    width: 20%;
}}

.logo-box {{
    border: 1px solid #ddd;
    min-height: 88px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 6px;
    background: white;
}}

.logo-fallback {{
    font-weight: bold;
    color: #9a7a2d;
    text-align: center;
    font-size: 20px;
}}

.report-table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 12px;
}}

.report-table th,
.report-table td {{
    border: 1px solid #444;
    padding: 8px 8px;
    vertical-align: middle;
}}

.report-table th {{
    background: #efefef;
    font-weight: bold;
    text-align: center;
    font-size: 12px;
}}

.center {{
    text-align: center;
}}

.footer-note {{
    margin-top: 10px;
    font-size: 11px;
    color: #666;
}}
</style>
</head>
<body>
<div class="page">

    <div class="header-wrap">
        <table class="header-table">
            <tr>
                <td class="header-label">A/C REG:</td>
                <td class="header-value">{html.escape(ac_reg)}</td>
                <td class="header-label">A/C TYPE:</td>
                <td class="header-value">{html.escape(ac_type)}</td>
                <td class="header-label">REV:</td>
                <td class="header-value">{html.escape(rev)}</td>
            </tr>
            <tr>
                <td class="header-label">A/C MSN:</td>
                <td class="header-value">{html.escape(msn)}</td>
                <td class="header-label">RJ REF:</td>
                <td class="header-value">{html.escape(rj_ref)}</td>
                <td class="header-label">DATE:</td>
                <td class="header-value">{html.escape(report_date)}</td>
            </tr>
        </table>

        <div class="logo-box">
            {logo_html}
        </div>
    </div>

    <table class="report-table">
        <tr>
            <th>ITEM</th>
            <th>SDR</th>
            <th>LOCATION</th>
            <th>DAMAGE / REPAIR TYPE</th>
            <th>REPAIR REFERENCE / ALLOWANCE</th>
            <th>DATE</th>
            <th>REMARKS</th>
            <th>SRI / INSPECTION / weight</th>
        </tr>
        {body_rows if body_rows else '<tr><td colspan="8" class="center">No records found.</td></tr>'}
    </table>

    <div class="footer-note">
        Report generated automatically from the SQLite assessment database.
    </div>
</div>
</body>
</html>
"""


def write_report_html(
    db_path: str | Path,
    out_path: str | Path,
    *,
    limit: Optional[int] = 25,
    ac_reg: str = "JY-REG",
    ac_type: str = "E195-E2",
    rev: str = "01",
    msn: str = "20180",
    rj_ref: str = "DBC-E195-E2-20180",
    report_date: Optional[str] = None,
    brand_name: str = "Royal Jordanian",
    logo_path: Optional[str | Path] = None,
) -> Path:
    rows = fetch_damage_rows(db_path, limit)
    html_text = render_report_html(
        rows,
        ac_reg=ac_reg,
        ac_type=ac_type,
        rev=rev,
        msn=msn,
        rj_ref=rj_ref,
        report_date=report_date,
        brand_name=brand_name,
        logo_path=logo_path,
    )
    out = Path(out_path)
    out.write_text(html_text, encoding="utf-8")
    return out


def write_report_pdf(
    db_path: str | Path,
    out_path: str | Path,
    *,
    limit: Optional[int] = 25,
    ac_reg: str = "JY-REG",
    ac_type: str = "E195-E2",
    rev: str = "01",
    msn: str = "20180",
    rj_ref: str = "DBC-E195-E2-20180",
    report_date: Optional[str] = None,
    brand_name: str = "Royal Jordanian",
    logo_path: Optional[str | Path] = None,
) -> Path:
    rows = fetch_damage_rows(db_path, limit)
    report_date = report_date or datetime.utcnow().strftime("%d.%m.%Y")
    out = Path(out_path)

    logo_file = Path(logo_path) if logo_path else DEFAULT_LOGO

    doc = SimpleDocTemplate(
        str(out),
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )

    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)
    small_center = ParagraphStyle("small_center", parent=small, alignment=TA_CENTER)

    story = []

    left_header_data = [
        [
            Paragraph(f"<b>A/C REG:</b> {html.escape(ac_reg)}", small),
            Paragraph(f"<b>A/C TYPE:</b> {html.escape(ac_type)}", small),
            Paragraph(f"<b>REV:</b> {html.escape(rev)}", small),
        ],
        [
            Paragraph(f"<b>A/C MSN:</b> {html.escape(msn)}", small),
            Paragraph(f"<b>RJ REF:</b> {html.escape(rj_ref)}", small),
            Paragraph(f"<b>DATE:</b> {html.escape(report_date)}", small),
        ],
    ]

    left_header = Table(left_header_data, colWidths=[60 * mm, 80 * mm, 38 * mm])
    left_header.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    if logo_file.exists():
        logo_el = Image(str(logo_file), width=42 * mm, height=24 * mm)
    else:
        logo_el = Paragraph(f"<b>{html.escape(brand_name)}</b>", small_center)

    header_outer = Table([[left_header, logo_el]], colWidths=[178 * mm, 48 * mm])
    header_outer.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    story.append(header_outer)
    story.append(Spacer(1, 5 * mm))

    table_data = [[
        Paragraph("<b>ITEM</b>", small_center),
        Paragraph("<b>SDR</b>", small_center),
        Paragraph("<b>LOCATION</b>", small_center),
        Paragraph("<b>DAMAGE / REPAIR TYPE</b>", small_center),
        Paragraph("<b>REPAIR REFERENCE / ALLOWANCE</b>", small_center),
        Paragraph("<b>DATE</b>", small_center),
        Paragraph("<b>REMARKS</b>", small_center),
        Paragraph("<b>SRI / INSPECTION / weight</b>", small_center),
    ]]

    for i, r in enumerate(rows, start=1):
        table_data.append([
            Paragraph(str(i), small_center),
            Paragraph(str(r.get("id", "")), small_center),
            Paragraph(html.escape(_location_text(r)), small),
            Paragraph(html.escape(_damage_type_text(r)), small),
            Paragraph(html.escape(_reference_text(r)), small),
            Paragraph(html.escape(_fmt_date(r.get("created_utc"))), small_center),
            Paragraph(html.escape(_remarks_text(r)), small),
            Paragraph(html.escape(_status_text(r)), small_center),
        ])

    report_table = Table(
        table_data,
        repeatRows=1,
        colWidths=[12 * mm, 18 * mm, 55 * mm, 40 * mm, 48 * mm, 23 * mm, 42 * mm, 28 * mm],
    )
    report_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDEDED")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (1, -1), "CENTER"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(report_table)

    doc.build(story)
    return out
