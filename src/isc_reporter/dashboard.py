"""Dashboard reports rendered to Excel (XlsxWriter) and PDF (reportlab).

A report module describes *what* to show in a :class:`Dashboard` (KPI tiles, charts, tables,
PDF sections, a read-me); this module decides *how*: one Excel workbook (Dashboard sheet with
tiles and native charts, one filterable table per sheet, Chart data, Read me) and one PDF
(tiles, charts, tables with page numbers).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import escape

from isc_reporter.errors import UsageError

GREEN, RED, GREY, BLUE, AMBER, DARK = "43A047", "E53935", "BDBDBD", "1E88E5", "FB8C00", "263238"
PALETTE = (BLUE, "8E24AA", "00897B", AMBER, "5E35B1", "6D4C41", "C0CA33", "546E7A")
TILE_COLOURS = (GREEN, RED, GREY, BLUE, AMBER)


@dataclass
class Chart:
    title: str
    kind: str                              # doughnut, bar, stacked
    categories: list[str]
    series: list[tuple[str, list, str]]    # (name, values, colour)
    percent: bool = False


@dataclass
class Table:
    """One Excel sheet. Column sets name the columns that get a special rendering."""

    name: str
    rows: list[dict]
    percent: set = field(default_factory=set)    # 0..1 shown as %
    wide: set = field(default_factory=set)       # wrapped long text
    bars: set = field(default_factory=set)       # 0..1 shown with a data bar
    red_yes: set = field(default_factory=set)    # "yes" in red
    red_positive: set = field(default_factory=set)  # > 0 in red
    text_colours: dict = field(default_factory=dict)  # column -> {starts with: green/red/grey}
    fills: dict = field(default_factory=dict)          # column -> {exact value: background hex}
    mono: set = field(default_factory=set)       # monospaced text (drawn trees)
    outline: str = ""                            # column holding each row's level: rows fold below it


@dataclass
class Section:
    """One PDF table: a subset of a table's columns."""

    title: str
    rows: list[dict]
    columns: list[str]


@dataclass
class Dashboard:
    title: str
    subtitle: str
    kpis: list[tuple[str, str, str]]       # (label, value, colour)
    charts: list[Chart]
    tables: list[Table]
    sections: list[Section] = field(default_factory=list)
    read_me: list[tuple[str, str]] = field(default_factory=list)
    colours: dict = field(default_factory=dict)        # fixed colour per chart category
    wide_pdf: set = field(default_factory=set)         # PDF columns sharing the free width
    alert: Callable[[dict], bool] | None = None        # PDF rows shown in red

    @property
    def percent(self) -> set:
        return set().union(*(t.percent for t in self.tables)) if self.tables else set()


def colour_for(dashboard: Dashboard, category: str, index: int = 0) -> str:
    """The category's fixed colour, otherwise a distinct palette colour."""
    return dashboard.colours.get(category, PALETTE[index % len(PALETTE)])


def subtitle(*parts: str) -> str:
    return "  |  ".join([f"Generated {datetime.now():%Y-%m-%d %H:%M}", *[p for p in parts if p]])


# -- Excel --------------------------------------------------------------------------


def write_xlsx(dashboard: Dashboard, path: str) -> str:
    try:
        import xlsxwriter
    except ImportError as exc:
        raise UsageError("Excel output needs XlsxWriter: pip install 'isc-reporter[excel]'") from exc

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Data is text, never formulas or links, whatever it starts with.
    book = xlsxwriter.Workbook(path, {"strings_to_formulas": False, "strings_to_urls": False,
                                      "strings_to_numbers": False})
    fmt = _formats(book)
    sheet = book.add_worksheet("Dashboard")
    names: set[str] = set()
    for table in dashboard.tables:
        _table(book, book.add_worksheet(table.name[:31]), table, fmt, _table_name(table.name, names))
    chart_data = book.add_worksheet("Chart data")
    _dashboard_sheet(book, sheet, chart_data, dashboard, fmt)
    if dashboard.read_me:
        _read_me(book.add_worksheet("Read me"), dashboard.read_me, fmt)
    sheet.activate()
    book.close()
    return path


def _formats(book) -> dict:
    def tile(colour, size, bold):
        return book.add_format({"bg_color": "#F5F7FA", "font_color": "#" + colour, "font_size": size,
                                "bold": bold, "align": "left", "valign": "vcenter", "indent": 1,
                                "left": 5, "left_color": "#" + colour})
    return {
        "title": book.add_format({"bold": True, "font_size": 20, "font_color": "#" + DARK}),
        "subtitle": book.add_format({"italic": True, "font_color": "#78909C"}),
        "header": book.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#" + DARK,
                                   "valign": "top", "text_wrap": True}),
        "percent": book.add_format({"num_format": "0%", "valign": "top"}),
        "wrap": book.add_format({"text_wrap": True, "valign": "top"}),
        "mono": book.add_format({"text_wrap": True, "valign": "top", "font_name": "Consolas"}),
        "top": book.add_format({"valign": "top"}),
        "bold": book.add_format({"bold": True}),
        "red": book.add_format({"font_color": "#C62828", "bold": True}),
        "green": book.add_format({"font_color": "#2E7D32"}),
        "grey": book.add_format({"font_color": "#757575"}),
        "tile_label": {c: tile(c, 9, False) for c in TILE_COLOURS},
        "tile_value": {c: tile(c, 16, True) for c in TILE_COLOURS},
    }


def _dashboard_sheet(book, sheet, chart_data, dashboard: Dashboard, fmt) -> None:
    sheet.hide_gridlines(2)
    sheet.set_column(0, 0, 2)
    sheet.set_column(1, 16, 11)
    sheet.write(0, 1, dashboard.title, fmt["title"])
    sheet.write(1, 1, dashboard.subtitle, fmt["subtitle"])
    for index, (label, value, colour) in enumerate(dashboard.kpis):
        row, col = 3 + (index // 4) * 3, 1 + (index % 4) * 4
        colour = colour if colour in TILE_COLOURS else BLUE
        sheet.merge_range(row, col, row, col + 3, label, fmt["tile_label"][colour])
        sheet.merge_range(row + 1, col, row + 1, col + 3, value, fmt["tile_value"][colour])
        sheet.set_row(row + 1, 26)

    first_chart_row = 4 + ((len(dashboard.kpis) + 3) // 4) * 3
    data_row = 0
    for index, chart in enumerate(dashboard.charts):
        # Chart source: a small table on the 'Chart data' sheet.
        chart_data.write(data_row, 0, chart.title, fmt["bold"])
        chart_data.write_row(data_row + 1, 0, ["Category"] + [s[0] for s in chart.series], fmt["header"])
        for offset, category in enumerate(chart.categories, start=data_row + 2):
            chart_data.write_string(offset, 0, category)
            for column, (_, values, _) in enumerate(chart.series, start=1):
                chart_data.write_number(offset, column, values[offset - data_row - 2],
                                        fmt["percent"] if chart.percent else None)
        first, last = data_row + 2, data_row + 1 + len(chart.categories)
        xl_chart = _chart(book, dashboard, chart, "Chart data", first, last)
        row, col = first_chart_row + (index // 2) * 18, 1 + (index % 2) * 8
        sheet.insert_chart(row, col, xl_chart, {"x_scale": 1.25, "y_scale": 1.2, "object_position": 1})
        data_row = last + 3
    chart_data.set_column(0, 0, 45)
    chart_data.set_column(1, 4, 12)


def _chart(book, dashboard: Dashboard, chart: Chart, sheet_name: str, first: int, last: int):
    categories = [sheet_name, first, 0, last, 0]
    if chart.kind == "doughnut":
        xl = book.add_chart({"type": "doughnut"})
        colours = [colour_for(dashboard, c, i) for i, c in enumerate(chart.categories)]
        xl.add_series({"name": chart.series[0][0], "categories": categories,
                       "values": [sheet_name, first, 1, last, 1],
                       "points": [{"fill": {"color": "#" + c}} for c in colours],
                       "data_labels": {"value": True, "percentage": False, "font": {"bold": True}}})
        xl.set_hole_size(55)
        xl.set_legend({"position": "right"})
    else:
        xl = book.add_chart({"type": "bar", "subtype": "stacked"} if chart.kind == "stacked"
                            else {"type": "bar"})
        for column, (name, _, colour) in enumerate(chart.series, start=1):
            xl.add_series({"name": name, "categories": categories,
                           "values": [sheet_name, first, column, last, column],
                           "fill": {"color": "#" + (colour or BLUE)}, "gap": 60,
                           "data_labels": {"value": True, "num_format": "0%" if chart.percent else "0",
                                           "font": {"color": "#FFFFFF" if chart.kind == "stacked"
                                                    else "#" + DARK, "size": 8}}})
        xl.set_y_axis({"reverse": True, "major_gridlines": {"visible": False}})
        axis = {"num_format": "0%" if chart.percent else "0",
                "major_gridlines": {"visible": True, "line": {"color": "#ECEFF1"}}}
        if chart.percent:
            axis["max"] = 1
        xl.set_x_axis(axis)
        xl.set_legend({"position": "bottom"} if len(chart.series) > 1 else {"none": True})
    xl.set_title({"name": chart.title, "name_font": {"size": 11, "color": "#" + DARK}})
    xl.set_chartarea({"border": {"none": True}})
    return xl


def _table_name(name: str, used: set[str]) -> str:
    """Excel table names: letters, digits and underscores, unique, never like a cell reference
    (``HR1``), hence the prefix."""
    base = "tbl_" + ("".join(c if c.isalnum() else "_" for c in name).strip("_") or "data")[:200]
    candidate, counter = base, 2
    while candidate.lower() in used:
        candidate, counter = f"{base}_{counter}", counter + 1
    used.add(candidate.lower())
    return candidate


def _table(book, sheet, table: Table, fmt, name: str) -> None:
    rows = table.rows
    if not rows:
        sheet.write(0, 0, f"No {table.name.lower()} in this report.", fmt["grey"])
        return
    headers = list(rows[0])
    for index, header in enumerate(headers):
        if header in table.mono:
            width, cell_format = 60, fmt["mono"]
        else:
            width = 45 if header in table.wide else max(10, min(30, len(header) + 4))
            cell_format = fmt["percent"] if header in table.percent else \
                fmt["wrap"] if header in table.wide else fmt["top"]
        sheet.set_column(index, index, width, cell_format)
    values = [["" if r.get(h) is None else r.get(h) for h in headers] for r in rows]
    sheet.add_table(0, 0, len(rows), len(headers) - 1, {
        "name": name,
        "style": "Table Style Light 9", "data": values,
        "columns": [{"header": h, "header_format": fmt["header"],
                     **({"format": fmt["percent"]} if h in table.percent else {})} for h in headers]})
    sheet.freeze_panes(1, 1)
    if table.outline:
        sheet.outline_settings(True, False, True, False)      # the fold button on the row above
        for index, row in enumerate(rows, start=1):
            if row.get(table.outline):
                sheet.set_row(index, None, None, {"level": min(int(row[table.outline]), 7)})
    last = len(rows)
    for index, header in enumerate(headers):
        if header in table.bars:
            sheet.conditional_format(1, index, last, index, {
                "type": "data_bar", "bar_color": "#" + BLUE, "bar_solid": True,
                "min_type": "num", "min_value": 0, "max_type": "num", "max_value": 1})
        if header in table.red_yes:
            sheet.conditional_format(1, index, last, index, {
                "type": "cell", "criteria": "==", "value": '"yes"', "format": fmt["red"]})
        if header in table.red_positive:
            sheet.conditional_format(1, index, last, index, {
                "type": "cell", "criteria": ">", "value": 0, "format": fmt["red"]})
        for text, colour in table.text_colours.get(header, {}).items():
            sheet.conditional_format(1, index, last, index, {
                "type": "text", "criteria": "begins with", "value": text, "format": fmt[colour]})
        for value, colour in table.fills.get(header, {}).items():
            key = f"fill_{colour}"
            if key not in fmt:
                fmt[key] = book.add_format({"bg_color": "#" + colour})
            sheet.conditional_format(1, index, last, index, {
                "type": "cell", "criteria": "==", "value": f'"{value}"', "format": fmt[key]})


def _read_me(sheet, lines, fmt) -> None:
    for number, (term, text) in enumerate(lines):
        sheet.write(number, 0, term, fmt["bold"])
        sheet.write(number, 1, text, fmt["wrap"])
    sheet.set_column(0, 0, 22)
    sheet.set_column(1, 1, 110)


# -- PDF ------------------------------------------------------------------------------


def write_pdf(dashboard: Dashboard, path: str, max_rows: int = 500) -> str:
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
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=7, leading=8.5)
    alert = ParagraphStyle("alert", parent=small, textColor=colors.HexColor("#C62828"))
    head = ParagraphStyle("head", parent=small, fontSize=7.5, textColor=colors.white, wordWrap=None)
    grey = ParagraphStyle("grey", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#78909C"))
    h1 = ParagraphStyle("h1", parent=styles["Title"], alignment=0, fontSize=20,
                        textColor=colors.HexColor("#" + DARK))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=colors.HexColor("#" + DARK))
    title = dashboard.title

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(margin, 7 * mm, f"{title}  |  {datetime.now():%Y-%m-%d %H:%M}")
        canvas.drawRightString(page[0] - margin, 7 * mm, f"Page {doc.page}")
        canvas.restoreState()

    story = [Paragraph(escape(title), h1), Paragraph(escape(dashboard.subtitle), grey), Spacer(1, 8)]
    tiles = []
    for label, value, colour in dashboard.kpis:
        tiles.append(Table([[Paragraph(escape(label), ParagraphStyle(
            "l", parent=small, textColor=colors.HexColor("#607D8B")))],
            [Paragraph(f"<b>{escape(value)}</b>", ParagraphStyle(
                "v", parent=styles["Normal"], fontSize=12, leading=15,
                textColor=colors.HexColor("#" + colour)))]],
            colWidths=[usable / 4 - 8], style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5F7FA")),
                ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor("#" + colour)),
                ("LEFTPADDING", (0, 0), (-1, -1), 8)])))
    if tiles:
        rows = [tiles[i:i + 4] + [""] * (4 - len(tiles[i:i + 4])) for i in range(0, len(tiles), 4)]
        grid = Table(rows, colWidths=[usable / 4] * 4)
        grid.setStyle(TableStyle([("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story += [grid, Spacer(1, 10)]

    drawings = [_pdf_chart(dashboard, chart, usable / 2 - 10) for chart in dashboard.charts]
    pairs = [drawings[i:i + 2] + [""] * (2 - len(drawings[i:i + 2])) for i in range(0, len(drawings), 2)]
    if pairs:
        chart_grid = Table(pairs, colWidths=[usable / 2] * 2)
        chart_grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                        ("BOTTOMPADDING", (0, 0), (-1, -1), 12)]))
        story.append(chart_grid)

    percent = dashboard.percent
    for section in dashboard.sections:
        rows, columns = section.rows, section.columns
        story += [Spacer(1, 6), CondPageBreak(60 * mm), Paragraph(escape(section.title), h2)]
        if not rows:
            story.append(Paragraph("None.", grey))
            continue
        if len(rows) > max_rows:
            story.append(Paragraph(escape(f"First {max_rows} of {len(rows)}; the Excel file has all."), grey))
        widths = _pdf_widths(columns, rows[:200], usable, dashboard.wide_pdf, percent)

        def row_style(row):
            return alert if dashboard.alert and dashboard.alert(row) else small

        table = Table([[Paragraph(f"<b>{escape(c)}</b>", head) for c in columns]] +
                      [[Paragraph(_pdf_markup(r.get(c), c, percent), row_style(r)) for c in columns]
                       for r in rows[:max_rows]],
                      colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + DARK)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#CFD8DC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
        story.append(table)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    SimpleDocTemplate(path, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=margin,
                      bottomMargin=14 * mm, title=title).build(story, onFirstPage=footer,
                                                                onLaterPages=footer)
    return path


def _pdf_widths(columns: list[str], rows: list[dict], usable: float, wide_columns: set,
                percent: set) -> list[float]:
    """Narrow columns get exactly the width of their longest word (header bold, cells regular),
    so nothing splits mid-word; wide text columns share what is left."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    padding = 8
    widths: list[float | None] = []
    for column in columns:
        if column in wide_columns:
            widths.append(None)
            continue
        words = [(w, "Helvetica-Bold", 7.5) for w in column.split()]
        words += [(w, "Helvetica", 7) for r in rows
                  for w in pdf_value(r.get(column), column, percent).split()]
        widths.append(max(stringWidth(w, font, size) for w, font, size in words) + padding)
    fixed = sum(w for w in widths if w is not None)
    wide = [i for i, w in enumerate(widths) if w is None]
    free = usable - fixed
    if wide and free < 60 * len(wide):       # too many columns: shrink the narrow ones
        scale = (usable - 60 * len(wide)) / fixed
        widths = [w * scale if w is not None else None for w in widths]
        free = 60 * len(wide)
    elif not wide:
        return [w * usable / fixed for w in widths]
    return [w if w is not None else free / len(wide) for w in widths]


def pdf_value(value, column: str, percent: set) -> str:
    if value is None or value == "":
        return "-" if column in percent else ""
    if column in percent:
        return f"{value:.0%}"
    return str(value)


def _pdf_markup(value, column: str, percent: set) -> str:
    return escape(pdf_value(value, column, percent)).replace("\n", "<br/>")


def _pdf_chart(dashboard: Dashboard, chart: Chart, width: float):
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.charts.doughnut import Doughnut
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors

    title_colour = colors.HexColor("#" + DARK)
    if chart.kind == "doughnut":
        values = chart.series[0][1]
        height = 170
        drawing = Drawing(width, height)
        drawing.add(String(0, height - 12, chart.title, fontName="Helvetica-Bold", fontSize=10,
                           fillColor=title_colour))
        doughnut = Doughnut()
        doughnut.x, doughnut.y, doughnut.width, doughnut.height = 10, 10, 130, 130
        doughnut.data = [v or 0.0001 for v in values]
        doughnut.innerRadiusFraction = 0.55
        doughnut.slices.strokeColor = colors.white
        doughnut.slices.strokeWidth = 1.5
        for index, category in enumerate(chart.categories):
            doughnut.slices[index].fillColor = colors.HexColor("#" + colour_for(dashboard, category, index))
        drawing.add(doughnut)
        total = sum(values) or 1
        drawing.add(String(75, 72, str(sum(values)), fontName="Helvetica-Bold", fontSize=14,
                           textAnchor="middle", fillColor=title_colour))
        legend = Legend()
        legend.x, legend.y = 160, height - 40
        legend.fontSize, legend.dy, legend.deltay = 8, 7, 14
        legend.colorNamePairs = [(colors.HexColor("#" + colour_for(dashboard, c, i)),
                                  f"{c}: {v} ({v / total:.0%})")
                                 for i, (c, v) in enumerate(zip(chart.categories, values, strict=True))]
        drawing.add(legend)
        return drawing

    # Thinner bars when there are many categories, so a chart fits beside the tiles on page 1.
    bar_height = min(13 if chart.kind == "stacked" else 11, max(6, 230 / max(1, len(chart.categories))))
    label_width = min(140, width * 0.4)
    height = len(chart.categories) * bar_height + 60
    drawing = Drawing(width, height)
    drawing.add(String(0, height - 12, chart.title, fontName="Helvetica-Bold", fontSize=10,
                       fillColor=title_colour))
    bars = HorizontalBarChart()
    bars.x, bars.y = label_width, 25
    bars.width, bars.height = width - label_width - 20, len(chart.categories) * bar_height
    bars.data = [list(reversed(values)) for _, values, _ in chart.series]
    bars.categoryAxis.categoryNames = list(reversed(chart.categories))
    bars.categoryAxis.labels.fontSize = 7
    bars.categoryAxis.labels.boxAnchor = "e"
    bars.categoryAxis.strokeColor = None
    bars.valueAxis.valueMin = 0
    if chart.percent:
        bars.valueAxis.valueMax = 1
        bars.valueAxis.labelTextFormat = lambda v: f"{v:.0%}"
    bars.valueAxis.labels.fontSize = 7
    bars.valueAxis.visibleGrid = True
    bars.valueAxis.gridStrokeColor = colors.HexColor("#ECEFF1")
    bars.valueAxis.strokeColor = None
    if chart.kind == "stacked":
        bars.categoryAxis.style = "stacked"
    bars.barSpacing = 1
    for index, (_, _, colour) in enumerate(chart.series):
        bars.bars[index].fillColor = colors.HexColor("#" + (colour or BLUE))
        bars.bars[index].strokeColor = None
    drawing.add(bars)
    if len(chart.series) > 1:
        legend = Legend()
        legend.x, legend.y = label_width, 10
        legend.alignment, legend.columnMaximum = "right", 1
        legend.fontSize, legend.deltax = 7, 70
        legend.colorNamePairs = [(colors.HexColor("#" + (c or BLUE)), n) for n, _, c in chart.series]
        drawing.add(legend)
    return drawing
