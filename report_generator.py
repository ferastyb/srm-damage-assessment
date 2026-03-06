# report_generator.py
# Generates clean SRM damage assessment reports from assessments.db
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import List, Dict, Optional, Any
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
)
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle


def fetch_damage_rows(db_path: str | Path, limit: Optional[int] = 25) -> List[Dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        q = """
        SELECT id, created_utc, aircraft_type, structure, structure_zone,
               side, sta, wl, stringer, frame,
               damage_type, dent_diameter_mm, dent_depth_mm,
               input_text, rules_json, srm_hits_json, result_json
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
    if r.get("side") and r["side"] != "ANY":
        bits.append(str(r["side"]))
    if r.get("structure"):
        bits.append(str(r["structure"]).replace("_", " "))
    if r.get("structure_zone"):
        bits.append(str(r["structure_zone"]).replace("_", " "))
    if r.get("frame") not in (None, ""):
        bits.append(f"(FR{r['frame']})")
    if r.get("stringer"):
        bits.append(f"(STGR #{r['stringer']})")
    if r.get("sta") not in (None, ""):
        try:
            bits.append(f"STA {float(r['sta']):g}")
        except Exception:
            bits.append(f"STA {r['sta']}")
    return " ".join(bits) if bits else "-"


def _damage_type_text(r: Dict[str, Any]) -> str:
    damage_type = str(r.get("damage_type") or "DAMAGE")
    extras: List[str] = []
    if r.get("dent_diameter_mm") not in (None, ""):
        extras.append(f"Dia {float(r['dent_diameter_mm']):g} mm")
    if r.get("dent_depth_mm") not in (None, ""):
        extras.append(f"Depth {float(r['dent_depth_mm']):g} mm")
    if extras:
        return f"{damage_type} ({', '.join(extras)})"
    return damage_type


def _reference_text(r: Dict[str, Any]) -> str:
    # Keep simple and safe for presentation
    if r.get("structure"):
        return "SRM Assessment Tool"
    return "-"


def _remarks_text(r: Dict[str, Any]) -> str:
    raw = (r.get("input_text") or "").strip()
    if raw:
        return "Auto-generated from logged assessment"
    return "-"


def _status_text(r: Dict[str, Any]) -> str:
    return "Closed"


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
) -> str:
    if not report_date:
        report_date = datetime.utcnow().strftime("%d.%m.%Y")

    body_rows = ""
    for i, r in enumerate(rows, start=1):
        body_rows += f"""
        <tr>
            <td>{i}</td>
            <td>{r.get('id','')}</td>
            <td>{_location_text(r)}</td>
            <td>{_damage_type_text(r)}</td>
            <td>{_reference_text(r)}</td>
            <td>{_fmt_date(r.get('created_utc'))}</td>
            <td>{_remarks_text(r)}</td>
            <td>{_status_text(r)}</td>
        </tr>
        """

    return f"""
<html>
<head>
<style>
body {{ font-family: Arial, sans-serif; }}
table {{ border-collapse: collapse; width:100%; }}
th, td {{ border:1px solid black; padding:6px; font-size:13px; vertical-align: top; }}
th {{ background:#f0f0f0; }}
.header-table td {{ border:none; padding:4px; }}
</style>
</head>
<body>
<h2>Structural Repair Status Report</h2>

<table class="header-table">
<tr>
<td><b>A/C REG:</b> {ac_reg}</td>
<td><b>A/C TYPE:</b> {ac_type}</td>
<td><b>REV:</b> {rev}</td>
</tr>
<tr>
<td><b>A/C MSN:</b> {msn}</td>
<td><b>RJ REF:</b> {rj_ref}</td>
<td><b>DATE:</b> {report_date}</td>
</tr>
</table>

<br>

<table>
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
{body_rows}
</table>
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
) -> Path:
    rows = fetch_damage_rows(db_path, limit)
    html = render_report_html(
        rows,
        ac_reg=ac_reg,
        ac_type=ac_type,
        rev=rev,
        msn=msn,
        rj_ref=rj_ref,
        report_date=report_date,
        brand_name=brand_name,
    )
    out = Path(out_path)
    out.write_text(html, encoding="utf-8")
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
) -> Path:
    rows = fetch_damage_rows(db_path, limit)
    report_date = report_date or datetime.utcnow().strftime("%d.%m.%Y")
    out = Path(out_path)

    doc = SimpleDocTemplate(
        str(out),
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = styles["Heading2"]
    title_style.alignment = TA_CENTER
    normal = styles["Normal"]
    small = ParagraphStyle("small", parent=normal, fontSize=8, leading=10)
    small_center = ParagraphStyle("small_center", parent=small, alignment=TA_CENTER)

    story = []
    story.append(Paragraph("Structural Repair Status Report", title_style))
    story.append(Spacer(1, 4 * mm))

    header_data = [
        [f"<b>A/C REG:</b> {ac_reg}", f"<b>A/C TYPE:</b> {ac_type}", f"<b>REV:</b> {rev}", f"<b>Brand:</b> {brand_name}"],
        [f"<b>A/C MSN:</b> {msn}", f"<b>RJ REF:</b> {rj_ref}", f"<b>DATE:</b> {report_date}", ""],
    ]
    header_table = Table(header_data, colWidths=[65 * mm, 65 * mm, 35 * mm, 80 * mm])
    header_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.append(header_table)
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
            Paragraph(_location_text(r), small),
            Paragraph(_damage_type_text(r), small),
            Paragraph(_reference_text(r), small),
            Paragraph(_fmt_date(r.get("created_utc")), small_center),
            Paragraph(_remarks_text(r), small),
            Paragraph(_status_text(r), small_center),
        ])

    report_table = Table(
        table_data,
        repeatRows=1,
        colWidths=[12 * mm, 18 * mm, 58 * mm, 48 * mm, 48 * mm, 22 * mm, 42 * mm, 28 * mm],
    )
    report_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDEDED")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (1, -1), "CENTER"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(report_table)

    doc.build(story)
    return out
