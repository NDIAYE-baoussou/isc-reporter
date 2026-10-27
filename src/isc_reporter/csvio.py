"""Reading CSV inputs (canonical column names plus aliases) and writing CSV outputs."""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from isc_reporter.errors import UsageError

log = logging.getLogger("isc_reporter")

ENCODINGS = ("utf-8-sig", "cp1252")


@dataclass(frozen=True)
class Column:
    """A canonical input column and the other header names accepted for it.

    With ``multi=True`` several source columns may feed the same canonical column
    (e.g. ``approbateur_1``..``approbateur_3``); their values are joined with the
    list separator.
    """

    aliases: tuple[str, ...] = ()
    multi: bool = False


class Row(dict):
    """One input row keyed by canonical column name. ``line`` is the line number in the file."""

    def __init__(self, line: int, values: dict | None = None):
        super().__init__(values or {})
        self.line = line


@dataclass
class CsvFile:
    rows: list[Row]
    columns: set[str]
    delimiter: str


def detect_delimiter(header_line: str) -> str:
    """Pick the delimiter from the header line. Defaults to ';' for single-column files,
    so values containing commas (LDAP DNs) are never split."""
    counts = {d: header_line.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ";"


def read_rows(path: str, schema: dict[str, Column], *, delimiter: str | None = None,
              list_separator: str = "|") -> CsvFile:
    """Read *path* and map its headers onto the canonical names in *schema*.

    Header matching ignores case and surrounding spaces. Unknown headers are
    reported once (they usually mean a typo) and ignored.
    """
    text = _read_text(path)
    first_line = text.split("\n", 1)[0]
    if not first_line.strip():
        raise UsageError(f"'{path}' is empty or has no header line")
    delimiter = delimiter or detect_delimiter(first_line)

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader)
    mapping, unknown = _map_header(header, schema)
    if unknown:
        log.warning("Ignoring unknown column(s) in %s: %s", path, ", ".join(unknown))

    rows = []
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue
        row = Row(reader.line_num)
        for index, canonical in mapping.items():
            value = cells[index].strip() if index < len(cells) else ""
            previous = row.get(canonical, "")
            if value and previous and schema[canonical].multi:
                row[canonical] = f"{previous}{list_separator}{value}"
            elif value and not previous:
                row[canonical] = value
            else:
                row.setdefault(canonical, "")
        rows.append(row)
    return CsvFile(rows=rows, columns=set(mapping.values()), delimiter=delimiter)


def write_table(path: str, columns: list[str], rows: list[list], delimiter: str = ",") -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        writer.writerow(columns)
        writer.writerows(rows)
    return path


def read_table(path: str, delimiter: str | None = None) -> tuple[list[str], list[dict]]:
    """Read a CSV produced by this tool (or any CSV) as plain dicts keyed by header."""
    text = _read_text(path)
    delimiter = delimiter or detect_delimiter(text.split("\n", 1)[0])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]
    return list(reader.fieldnames or []), rows


def output_path(output_dir: str, stem: str, extension: str) -> str:
    """``<output_dir>/<stem>_<timestamp><extension>``, never overwriting an existing file."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = os.path.join(output_dir, f"{stem}_{timestamp}")
    candidate, counter = f"{base}{extension}", 1
    while os.path.exists(candidate):
        candidate = f"{base}_{counter}{extension}"
        counter += 1
    return candidate


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        raise UsageError(f"CSV file '{path}' does not exist")
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding != ENCODINGS[0]:
            log.warning("%s is not UTF-8, read it as %s", path, encoding)
        return text.replace("\r\n", "\n").replace("\r", "\n")
    raise UsageError(f"Cannot decode '{path}'; save it as UTF-8")  # pragma: no cover


def _map_header(header: list[str], schema: dict[str, Column]) -> tuple[dict[int, str], list[str]]:
    lookup = {}
    for canonical, column in schema.items():
        for name in (canonical, *column.aliases):
            lookup[name.strip().lower()] = canonical
    mapping, unknown = {}, []
    for index, name in enumerate(header):
        key = name.strip().lower()
        if key in lookup:
            mapping[index] = lookup[key]
        elif key:
            unknown.append(name.strip())
    return mapping, unknown
