"""Regex-based extraction of constants and function names from JavaScript files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class JSConstantDef:
    name: str
    value: str
    file: str
    line: int


@dataclass
class JSFunctionDef:
    name: str
    file: str
    line: int
    is_async: bool = False


@dataclass
class JSFileInfo:
    constants: list[JSConstantDef] = field(default_factory=list)
    functions: list[JSFunctionDef] = field(default_factory=list)


# const UPPER_NAME = value;
_CONST_RE = re.compile(
    r'^(?:const|let|var)\s+([A-Z][A-Z_0-9]+)\s*=\s*(.+?)\s*;?\s*$'
)

# function name(...) or async function name(...)
_FUNC_RE = re.compile(
    r'^(async\s+)?function\s+(\w+)\s*\('
)

# const name = (...) => or const name = async (...) =>
_ARROW_RE = re.compile(
    r'^(?:const|let|var)\s+(\w+)\s*=\s*(async\s+)?(?:\([^)]*\)|[a-zA-Z_]\w*)\s*=>'
)


def extract_file(filepath: Path) -> JSFileInfo:
    """Extract constants and function definitions from a JS file."""
    info = JSFileInfo()
    try:
        lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return info

    rel = filepath.as_posix()

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()

        # Constants (UPPER_CASE)
        m = _CONST_RE.match(stripped)
        if m:
            info.constants.append(JSConstantDef(
                name=m.group(1),
                value=m.group(2).strip(),
                file=rel,
                line=i,
            ))
            continue

        # Named function declarations
        m = _FUNC_RE.match(stripped)
        if m:
            info.functions.append(JSFunctionDef(
                name=m.group(2),
                file=rel,
                line=i,
                is_async=bool(m.group(1)),
            ))
            continue

        # Arrow functions assigned to named variables
        m = _ARROW_RE.match(stripped)
        if m:
            name = m.group(1)
            # Skip UPPER_CASE — those are constants, not functions
            if not name.isupper():
                info.functions.append(JSFunctionDef(
                    name=name,
                    file=rel,
                    line=i,
                    is_async=bool(m.group(2)),
                ))

    return info


def extract_directory(root: Path) -> dict[str, JSFileInfo]:
    """Extract info from all .js files under root. Returns {relative_path: info}."""
    results: dict[str, JSFileInfo] = {}
    for filepath in root.rglob("*.js"):
        info = extract_file(filepath)
        if info.constants or info.functions:
            results[filepath.as_posix()] = info
    return results
