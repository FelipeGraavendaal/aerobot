"""Genera el PDF del informe de auditoría a partir de `pdf_payload` (ReportLab)."""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_pdf(payload: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
                            title=payload["title"], author="AeroBot")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=13)
    head = ParagraphStyle("h", parent=ss["Heading4"], spaceBefore=8, spaceAfter=2)
    indent = ParagraphStyle("i", parent=body, leftIndent=14)
    flow = [Paragraph(_esc(payload["title"]), ss["Heading3"]), Spacer(1, 4)]
    for line in payload["body"]:
        if not line.strip():
            flow.append(Spacer(1, 4))
        elif line[:2].strip().rstrip(".").isdigit() and line.isupper():
            flow.append(Paragraph(_esc(line), head))
        elif line.startswith("    "):
            flow.append(Paragraph(_esc(line.strip()), indent))
        else:
            flow.append(Paragraph(_esc(line), body))
    flow += [Spacer(1, 10), Paragraph(_esc(f"ID de auditoría {payload['audit_id']} · generado {payload['generated_at']}"),
                                      ParagraphStyle("f", parent=body, fontSize=8, textColor="#666666"))]
    doc.build(flow)
    return buf.getvalue()
