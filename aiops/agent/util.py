"""Small shared helpers: name validation, quantity parsing, time."""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Kubernetes DNS-1123 style names; used to validate anything that came from an alert.
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")

_UNITS = {
    "": 1, "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
}
_QTY_RE = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti|k|M|G|T)?$")


def valid_name(value: object) -> bool:
    return isinstance(value, str) and bool(NAME_RE.match(value))


def parse_quantity(value: str) -> int:
    """Parse a simple Kubernetes memory quantity (integer + optional suffix) into bytes."""
    m = _QTY_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"unsupported quantity: {value!r}")
    return int(m.group(1)) * _UNITS[m.group(2) or ""]


def format_mi(num_bytes: int) -> str:
    mi = max(1, -(-num_bytes // (1024**2)))  # ceil
    return f"{mi}Mi"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
