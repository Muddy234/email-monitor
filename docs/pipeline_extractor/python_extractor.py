"""AST-based extraction of constants and function signatures from Python files."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConstantDef:
    name: str
    value: str  # string representation
    file: str
    line: int


@dataclass
class FunctionDef:
    name: str
    args: list[str]
    file: str
    line: int
    is_async: bool = False


@dataclass
class PythonFileInfo:
    constants: list[ConstantDef] = field(default_factory=list)
    functions: list[FunctionDef] = field(default_factory=list)


def _is_constant_name(name: str) -> bool:
    """Check if a name looks like a module-level constant (UPPER_CASE)."""
    return name.isupper() and "_" in name or (name.isupper() and len(name) > 1)


def _value_to_str(node: ast.expr) -> str:
    """Best-effort string representation of an AST value node."""
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Dict):
        return "{...}"
    if isinstance(node, ast.List):
        return f"[...] ({len(node.elts)} items)"
    if isinstance(node, ast.Tuple):
        return f"(...) ({len(node.elts)} items)"
    if isinstance(node, ast.Call):
        func = _value_to_str(node.func)
        return f"{func}(...)"
    if isinstance(node, ast.Attribute):
        return f"{_value_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return f"-{_value_to_str(node.operand)}"
    if isinstance(node, ast.BinOp):
        left = _value_to_str(node.left)
        right = _value_to_str(node.right)
        if isinstance(node.op, ast.Mult):
            return f"{left} * {right}"
        if isinstance(node.op, ast.Add):
            return f"{left} + {right}"
        return f"{left} op {right}"
    return "<?>"


def extract_file(filepath: Path) -> PythonFileInfo:
    """Extract constants and function signatures from a Python file."""
    info = PythonFileInfo()
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (OSError, SyntaxError):
        return info

    rel = filepath.as_posix()

    for node in ast.iter_child_nodes(tree):
        # Module-level constant assignments
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_constant_name(target.id):
                    info.constants.append(ConstantDef(
                        name=target.id,
                        value=_value_to_str(node.value),
                        file=rel,
                        line=node.lineno,
                    ))

        # Function definitions (top-level and in classes)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            info.functions.append(FunctionDef(
                name=node.name,
                args=args,
                file=rel,
                line=node.lineno,
                is_async=isinstance(node, ast.AsyncFunctionDef),
            ))

        # Class definitions — extract methods
        if isinstance(node, ast.ClassDef):
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in item.args.args if a.arg != "self"]
                    info.functions.append(FunctionDef(
                        name=f"{node.name}.{item.name}",
                        args=args,
                        file=rel,
                        line=item.lineno,
                        is_async=isinstance(item, ast.AsyncFunctionDef),
                    ))

    return info


def extract_directory(root: Path) -> dict[str, PythonFileInfo]:
    """Extract info from all .py files under root. Returns {relative_path: info}."""
    results: dict[str, PythonFileInfo] = {}
    for filepath in root.rglob("*.py"):
        info = extract_file(filepath)
        if info.constants or info.functions:
            results[filepath.as_posix()] = info
    return results
