"""Quoting for ISC search queries and collection filters."""


def quote(value) -> str:
    """Return *value* as a double-quoted literal, safe to embed in a search query
    (``name.exact:<here>``) or a list filter (``name eq <here>``)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
