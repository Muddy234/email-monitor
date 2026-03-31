"""Parser for @pipeline markers in Python and JavaScript source files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MarkerRecord:
    file: str
    line: int
    marker_type: str  # phase, step, gate, parallel, parallel-box, writes, reads, etc.
    attrs: dict[str, str] = field(default_factory=dict)


# Matches: # @pipeline key="value" key2="value2" key3=bare
# Also:   // @pipeline key="value" ...
_MARKER_LINE_RE = re.compile(
    r'(?:#|//)\s*@pipeline\s+(.*)', re.IGNORECASE
)

# Matches individual key=value or key="value" pairs
_ATTR_RE = re.compile(
    r'(\w[\w-]*)='
    r'(?:"([^"]*?)"|\'([^\']*?)\'|(\S+))'
)


def parse_marker_line(text: str) -> tuple[str, dict[str, str]] | None:
    """Parse a single @pipeline marker line into (type, attrs).

    The first key=value pair where key is a known type keyword determines
    the marker type. All pairs become attrs.
    """
    m = _MARKER_LINE_RE.search(text)
    if not m:
        return None

    raw = m.group(1)
    attrs: dict[str, str] = {}
    for am in _ATTR_RE.finditer(raw):
        key = am.group(1)
        val = am.group(2) if am.group(2) is not None else (
            am.group(3) if am.group(3) is not None else am.group(4)
        )
        attrs[key] = val

    if not attrs:
        return None

    # First key determines marker type
    marker_type = next(iter(attrs))
    return marker_type, attrs


def parse_file(filepath: Path) -> list[MarkerRecord]:
    """Parse all @pipeline markers from a single file."""
    records: list[MarkerRecord] = []
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records

    rel = filepath.as_posix()
    for i, line in enumerate(text.splitlines(), start=1):
        result = parse_marker_line(line)
        if result is None:
            continue
        marker_type, attrs = result
        records.append(MarkerRecord(
            file=rel,
            line=i,
            marker_type=marker_type,
            attrs=attrs,
        ))
    return records


def parse_directory(root: Path, extensions: tuple[str, ...] = (".py", ".js")) -> list[MarkerRecord]:
    """Recursively parse all matching files under root."""
    records: list[MarkerRecord] = []
    for ext in extensions:
        for filepath in root.rglob(f"*{ext}"):
            records.extend(parse_file(filepath))
    return records
