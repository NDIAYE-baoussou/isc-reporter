"""Render a :class:`isc_reporter.reporting.Report` as Excel (with charts), PDF (with charts) or CSV."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from xml.sax.saxutils import escape

from isc_reporter.errors import UsageError
from isc_reporter.reporting import LIST_SEP, Report

DARK, LIGHT, ACCENT = "2C3E50", "EEF2F7", "4472C4"
MAX_CELL = 32000           # Excel's limit is 32,767 characters
PDF_MAX_LIST_ITEMS = 12
PDF_MAX_CELL = 600
SECTION_ROWS = 18          # rows reserved per breakdown on the Excel summary (fits its chart)


def write_csv(report: Report, path: str) -> list[str]:
    paths = [_csv(path, report.headers, report.rows())]
    if report.expand:
        detail = os.path.splitext(path)[0] + f"-{report.expand.path}.csv"
        paths.append(_csv(detail, report.detail_headers(), report.detail_rows()))
    return paths


def _csv(path: str, headers: list[str], rows: list[list]) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)
    return path


# -- Excel ----------------------------------------------------------------------
# Written with XlsxWriter, which produces the same package structure as Excel itself (shared
# strings, string category references in charts, native tables), so every viewer renders it.


def write_xlsx(report: Report, path: str) -> str:
    try:
        import xlsxwriter
    except ImportError as exc:
        raise UsageError("Excel output needs XlsxWriter: pip install 'isc-reporter[excel]'") from exc

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Data is text, never formulas or links, whatever it starts with.
    book = xlsxwriter.Workbook(path, {"strings_to_formulas": False, "strings_to_urls": False,
                                      "strings_to_numbers": False})
    formats = _formats(book)
    _summary_sheet(book, book.add_worksheet("Summary"), report, formats)
    _data_sheet(book.add_worksheet("Data"), report.headers, report.rows(),
                [c.width for c in report.columns], "Results", formats)
    if report.expand:
        widths = [c.width for c in report.expand.parent + report.expand.item]
        _data_sheet(book.add_worksheet(f"Detail - {report.expand.title}"[:31]), report.detail_headers(),
                    report.detail_rows(), widths, "Detail", formats)
    _query_sheet(book.add_worksheet("Query"), report, formats)
    book.close()
    return path


def _formats(book) -> dict:
    return {
        "title": book.add_format({"bold": True, "font_size": 16, "font_color": "#" + DARK}),
        "subtitle": book.add_format({"italic": True, "font_color": "#808080"}),
        "section": book.add_format({"bold": True, "font_size": 12, "font_color": "#" + DARK}),
        "header": book.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#" + DARK,
                                   "valign": "top", "text_wrap": True}),
        "label": book.add_format({"bold": True, "valign": "top"}),
        "left": book.add_format({"align": "left", "valign": "top"}),
        "percent": book.add_format({"num_format": "0%"}),
        "wrap": book.add_format({"text_wrap": True, "valign": "top"}),
        "top": book.add_format({"valign": "top"}),
    }


def _summary_sheet(book, sheet, report: Report, formats: dict) -> None:
    sheet.write(0, 0, report.title, formats["title"])
    sheet.write(1, 0, _subtitle(report), formats["subtitle"])
    row = 3
    sheet.write(row, 0, "Key figures", formats["section"])
    sheet.write_row(row + 1, 0, ["Figure", "Value"], formats["header"])
    figures = report.figures()
    for offset, (label, value) in enumerate(figures, start=row + 2):
        sheet.write(offset, 0, label)
        sheet.write(offset, 1, value, formats["left"])
    row += len(figures) + 4

    for breakdown in report.breakdowns:
        data = report.breakdown(breakdown)
        if not data:
            continue
        total = sum(n for _, n in data) or 1
        sheet.write(row, 0, breakdown.title, formats["section"])
        sheet.write_row(row + 1, 0, [breakdown.title, "Count", "Share"], formats["header"])
        for offset, (label, n) in enumerate(data, start=row + 2):
            sheet.write_string(offset, 0, label)
            sheet.write_number(offset, 1, n)
            sheet.write_number(offset, 2, n / total, formats["percent"])
        first, last = row + 2, row + 1 + len(data)
        sheet.insert_chart(row, 4, _chart(sheet, book, breakdown, len(data), first, last),
                           {"x_scale": 1.3, "y_scale": 1.0})
        row += max(len(data) + 4, SECTION_ROWS)

    sheet.set_column(0, 0, 42)
    sheet.set_column(1, 1, 14)
    sheet.set_column(2, 2, 9)


def _chart(sheet, book, breakdown, size: int, first: int, last: int):
    """Pie for a few categories, bars for rankings, columns for dates and ranges."""
    pie = size <= 6 and not breakdown.date and not breakdown.order
    if pie:
        chart = book.add_chart({"type": "pie"})
        labels = {"percentage": True, "category": False}
    else:
        kind = "column" if breakdown.date or breakdown.order else "bar"
        chart = book.add_chart({"type": kind})
        labels = {"value": True}
        chart.set_legend({"none": True})
        if kind == "bar":
            chart.set_y_axis({"reverse": True})   # biggest first, at the top
            chart.set_x_axis({"major_gridlines": {"visible": True, "line": {"color": "#DDDDDD"}}})
    name = sheet.get_name()
    series = {"name": breakdown.title, "categories": [name, first, 0, last, 0],
              "values": [name, first, 1, last, 1], "data_labels": labels}
    if not pie:
        series["fill"] = {"color": "#" + ACCENT}
    chart.add_series(series)
    chart.set_title({"name": breakdown.title, "name_font": {"size": 11}})
    return chart


def _data_sheet(sheet, headers: list[str], rows: list[list], widths: list[int], name: str,
                formats: dict) -> None:
    headers = _unique(headers)
    rows = [[v[:MAX_CELL] if isinstance(v, str) else v for v in values] for values in rows]
    for index, width in enumerate(widths):
        sheet.set_column(index, index, width, formats["wrap"] if width >= 40 else formats["top"])
    if rows:
        sheet.add_table(0, 0, len(rows), len(headers) - 1, {
            "name": name, "style": "Table Style Medium 2", "data": rows,
            "columns": [{"header": h, "header_format": formats["header"]} for h in headers]})
    else:
        sheet.write_row(0, 0, headers, formats["header"])
        sheet.write(1, 0, "No results")
    sheet.freeze_panes(1, 1)


def _query_sheet(sheet, report: Report, formats: dict) -> None:
    lines = [
        ("Report", report.report_type.name),
        ("Search index", report.report_type.index),
        ("Query", report.query),
        ("Columns", ", ".join(report.headers)),
        ("Grouped by", ", ".join(b.title for b in report.breakdowns)),
        ("Detail", report.expand.title if report.expand else "-"),
        ("Results", len(report.docs)),
        ("Generated", f"{datetime.now():%Y-%m-%d %H:%M}"),
        ("Tenant", report.origin or "-"),
    ]
    for number, (label, value) in enumerate(lines):
        sheet.write(number, 0, label, formats["label"])
        sheet.write(number, 1, value, formats["wrap"])
    sheet.set_column(0, 0, 16)
    sheet.set_column(1, 1, 110)


def _unique(headers: list[str]) -> list[str]:
    """Excel tables need distinct, non-empty column names."""
    seen: dict[str, int] = {}
    result = []
    for header in headers:
        header = header or "Column"
        seen[header.lower()] = seen.get(header.lower(), 0) + 1
        result.append(header if seen[header.lower()] == 1 else f"{header} ({seen[header.lower()]})")
    return result


def _subtitle(report: Report) -> str:
    where = f" on {report.origin}" if report.origin else ""
    return f"Generated {datetime.now():%Y-%m-%d %H:%M}{where}  |  query: {report.query}"


# -- PDF ------------------------------------------------------------------------


def write_pdf(report: Report, path: str, max_rows: int = 2000) -> str:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import CondPageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise UsageError("PDF output needs reportlab: pip install 'isc-reporter[pdf]'") from exc

    page = landscape(A4)
    margin = 12 * mm
    usable = page[0] - 2 * margin
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=7, leading=8.5, wordWrap="CJK")
    head = ParagraphStyle("head", parent=small, fontSize=7.5, textColor=colors.white, wordWrap=None)
    grey = ParagraphStyle("grey", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    generated = f"{datetime.now():%Y-%m-%d %H:%M}"

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(margin, 7 * mm, f"{report.title}  |  {generated}")
        canvas.drawRightString(page[0] - margin, 7 * mm, f"Page {doc.page}")
        canvas.restoreState()

    story = [Paragraph(escape(report.title), styles["Title"]), Paragraph(escape(_subtitle(report)), grey),
             Spacer(1, 6)]
    figures = Table([[Paragraph(f"<b>{escape(str(k))}</b>", small), Paragraph(escape(str(v)), small)]
                     for k, v in report.figures()], colWidths=[70 * mm, 60 * mm], hAlign="LEFT")
    figures.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                                 ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#" + LIGHT))]))
    story += [figures, Spacer(1, 8)]

    charts = [_pdf_chart(b.title, report.breakdown(b), usable / 2 - 6) for b in report.breakdowns]
    charts = [c for c in charts if c is not None]
    if charts:
        pairs = [charts[i:i + 2] + [""] * (2 - len(charts[i:i + 2])) for i in range(0, len(charts), 2)]
        grid = Table(pairs, colWidths=[usable / 2] * 2, hAlign="LEFT")
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(grid)

    columns = [c for c in report.columns if c.in_pdf] or report.columns
    weights = [c.width for c in columns]
    widths = [usable * w / sum(weights) for w in weights]
    rows = [[c.value(doc) for c in columns] for doc in report.docs[:max_rows]]
    data = [[Paragraph(f"<b>{escape(c.header)}</b>", head) for c in columns]]
    data += [[Paragraph(_pdf_text(v), small) for v in values] for values in rows]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + DARK)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#" + LIGHT)]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    # A new page only when the results would start too low on the current one.
    story += [Spacer(1, 10), CondPageBreak(70 * mm), Paragraph("Results", styles["Heading2"])]
    if len(report.docs) > max_rows:
        story.append(Paragraph(escape(f"First {max_rows} of {len(report.docs)} results; the Excel "
                                      "file has them all."), grey))
    hidden = [c.header for c in report.columns if c not in columns]
    if hidden:
        story.append(Paragraph(escape(f"More columns in the Excel file: {', '.join(hidden)}."), grey))
    story += [Spacer(1, 4), table]

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    SimpleDocTemplate(path, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=margin,
                      bottomMargin=14 * mm, title=report.title).build(
        story, onFirstPage=footer, onLaterPages=footer)
    return path


def _pdf_chart(title: str, data: list[tuple[str, int]], width: float):
    """Horizontal bar chart (reportlab graphics), largest category on top."""
    if not data:
        return None
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors

    bar_height = 11
    label_width = min(150, width * 0.45)
    height = len(data) * bar_height + 40
    drawing = Drawing(width, height)
    drawing.add(String(0, height - 12, title, fontName="Helvetica-Bold", fontSize=9,
                       fillColor=colors.HexColor("#" + DARK)))
    chart = HorizontalBarChart()
    chart.x, chart.y = label_width, 10
    chart.width, chart.height = width - label_width - 30, len(data) * bar_height
    chart.data = [[n for _, n in reversed(data)]]
    chart.categoryAxis.categoryNames = [_short(label, 34) for label, _ in reversed(data)]
    chart.categoryAxis.labels.fontSize = 6.5
    chart.categoryAxis.labels.boxAnchor = "e"
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontSize = 6.5
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeColor = colors.HexColor("#DDDDDD")
    chart.bars[0].fillColor = colors.HexColor("#" + ACCENT)
    chart.bars[0].strokeColor = None
    chart.barLabelFormat = "%d"
    chart.barLabels.fontSize = 6
    chart.barLabels.boxAnchor = "w"
    chart.barLabels.dx = 2
    drawing.add(chart)
    return drawing


def _pdf_text(value) -> str:
    from isc_reporter.reporting import text

    if isinstance(value, list):
        values = [text(v) for v in value if v not in (None, "")]
        if len(values) > PDF_MAX_LIST_ITEMS:
            values = values[:PDF_MAX_LIST_ITEMS] + [f"(+{len(values) - PDF_MAX_LIST_ITEMS} more)"]
        value = "\n".join(values)
    else:
        value = text(value).replace(LIST_SEP, "\n")
    if len(value) > PDF_MAX_CELL:
        value = value[:PDF_MAX_CELL] + " …"
    return escape(value).replace("\n", "<br/>")


def _short(label: str, size: int) -> str:
    return label if len(label) <= size else label[:size - 1] + "…"
