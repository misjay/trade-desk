"""
export_access_codes_pdf.py — Generates 1,000 unique single-use access codes,
persists them into access_codes.json, and exports an executive publication-quality PDF.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ReportLab imports
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

import access_manager

OUTPUT_PDF = Path(__file__).parent / "WINZ_ACCESS_CODES_1000.pdf"
OUTPUT_CSV = Path(__file__).parent / "WINZ_ACCESS_CODES_1000.csv"
OUTPUT_TXT = Path(__file__).parent / "WINZ_ACCESS_CODES_1000.txt"


class NumberedCanvas(canvas.Canvas):
    """Running header and 'Page X of Y' footer."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, total_pages: int):
        self.saveState()
        self.setFont("Helvetica-Bold", 7)
        self.setFillColor(colors.HexColor("#64748B"))

        # Header (Pages 2+)
        if self._pageNumber > 1:
            self.drawString(36, 762, "WINZ AUTONOMOUS TRADING DESK — VIP ACCESS DIRECTORY (1,000 SINGLE-USE CODES)")
            self.drawRightString(576, 762, "BATCH 01")
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(36, 756, 576, 756)

        # Footer (All pages)
        self.setFont("Helvetica", 7)
        self.drawString(36, 26, "CONFIDENTIAL & PROPRIETARY — FOR AUTHORIZED RECIPIENTS ONLY • 1 CODE PER PERSON")
        self.drawRightString(576, 26, f"Page {self._pageNumber} of {total_pages}")
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.setLineWidth(0.5)
        self.line(36, 36, 576, 36)
        self.restoreState()


def generate_and_export():
    print("Generating 1,000 unique single-use access codes...")
    
    # 1. Generate 1,000 unique single-use codes (max_uses=1)
    batch_records = access_manager.create_batch_access_codes(
        count=1000,
        max_uses=1,
        label="VIP Genesis Batch 01",
        created_by="Desk Master Admin",
    )
    print(f"Generated and persisted {len(batch_records)} unique single-use codes in access_codes.json")

    # 2. Export CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Index", "Access Code", "Direct Telegram Link", "Max Uses", "Status", "Batch Label"])
        for idx, rec in enumerate(batch_records, start=1):
            code = rec["code"]
            link = f"https://t.me/thewinzbot?start={code}"
            writer.writerow([idx, code, link, 1, "Active (Unused)", rec["label"]])
    print(f"Exported CSV: {OUTPUT_CSV}")

    # 3. Export TXT
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        for rec in batch_records:
            f.write(f"{rec['code']}\n")
    print(f"Exported TXT: {OUTPUT_TXT}")

    # 4. Generate Executive PDF
    print("Compiling Executive PDF...")
    doc = SimpleDocTemplate(
        str(OUTPUT_PDF),
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=46,
        bottomMargin=46,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0F172A"),
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#2563EB"),
    )
    body_style = ParagraphStyle(
        "BodyDark",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#334155"),
    )
    code_cell_style = ParagraphStyle(
        "CodeCell",
        parent=styles["Normal"],
        fontName="Courier-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#0F172A"),
    )
    link_cell_style = ParagraphStyle(
        "LinkCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        leading=9,
        textColor=colors.HexColor("#2563EB"),
    )
    num_cell_style = ParagraphStyle(
        "NumCell",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor("#64748B"),
    )

    story = []

    # Title Banner
    story.append(Paragraph("WINZ AUTONOMOUS TRADING DESK", subtitle_style))
    story.append(Spacer(1, 3))
    story.append(Paragraph("VIP Access Codes Directory — 1,000 Single-Use Keys", title_style))
    story.append(Spacer(1, 4))
    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y • %H:%M UTC")
    story.append(Paragraph(f"<b>Batch:</b> Genesis-01 &nbsp;|&nbsp; <b>Allocation:</b> 1 Code Per Person (Single Redemption) &nbsp;|&nbsp; <b>Generated:</b> {now_str}", body_style))
    story.append(Spacer(1, 10))

    # Instructions Box
    instructions_text = (
        "<b>RECIPIENT ACTIVATION INSTRUCTIONS:</b><br/>"
        "• <b>Option 1 (One-Click Link):</b> Send the user their unique URL: <code>https://t.me/thewinzbot?start=&lt;CODE&gt;</code>.<br/>"
        "• <b>Option 2 (Manual Command):</b> User opens <b>@thewinzbot</b> on Telegram and enters: <code>/access &lt;CODE&gt;</code>.<br/>"
        "• <b>Security Policy:</b> Each code activates exactly once. Upon redemption, it binds permanently to that user's Telegram ID."
    )
    inst_table = Table(
        [[Paragraph(instructions_text, body_style)]],
        colWidths=[540],
    )
    inst_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(inst_table)
    story.append(Spacer(1, 12))

    # Build 2-column paired table (each row has 2 codes: Left: [#, Code, Link] | Right: [#, Code, Link])
    # Total width = 540 pt
    # Col widths: [22, 100, 148,  22, 100, 148] = 540 pt
    table_data = [
        [
            Paragraph("<b>#</b>", num_cell_style),
            Paragraph("<b>ACCESS CODE</b>", num_cell_style),
            Paragraph("<b>ONE-CLICK TELEGRAM LINK</b>", num_cell_style),
            Paragraph("<b>#</b>", num_cell_style),
            Paragraph("<b>ACCESS CODE</b>", num_cell_style),
            Paragraph("<b>ONE-CLICK TELEGRAM LINK</b>", num_cell_style),
        ]
    ]

    total = len(batch_records)
    half = (total + 1) // 2

    for i in range(half):
        # Left item
        rec_left = batch_records[i]
        num_l = f"#{i+1:04d}"
        code_l = rec_left["code"]
        link_l = f"t.me/thewinzbot?start={code_l}"

        # Right item
        j = i + half
        if j < total:
            rec_right = batch_records[j]
            num_r = f"#{j+1:04d}"
            code_r = rec_right["code"]
            link_r = f"t.me/thewinzbot?start={code_r}"
        else:
            num_r = ""
            code_r = ""
            link_r = ""

        row = [
            Paragraph(num_l, num_cell_style),
            Paragraph(code_l, code_cell_style),
            Paragraph(link_l, link_cell_style),
            Paragraph(num_r, num_cell_style),
            Paragraph(code_r, code_cell_style),
            Paragraph(link_r, link_cell_style),
        ]
        table_data.append(row)

    col_widths = [24, 105, 141, 24, 105, 141]
    code_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    
    t_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
    ]

    # Alternate row colors
    for r in range(1, len(table_data)):
        if r % 2 == 0:
            t_style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))

    # Middle vertical separator between the two columns
    t_style.append(("LINEAFTER", (2, 0), (2, -1), 1.2, colors.HexColor("#94A3B8")))

    code_table.setStyle(TableStyle(t_style))
    story.append(code_table)

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Executive PDF built successfully: {OUTPUT_PDF} ({doc.page} pages)")
    return OUTPUT_PDF, OUTPUT_CSV, OUTPUT_TXT


if __name__ == "__main__":
    generate_and_export()
