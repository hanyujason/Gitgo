"""Precisely update the Host declaration in disposable two-version CI sources."""
from __future__ import annotations

import ast
import io
import json
import tokenize

from backend.core.update.version import Version


def successor_source(source: str, previous: str, candidate: str) -> str:
    """Replace one literal declaration, preserving all unrelated source bytes."""
    if Version.parse(candidate) <= Version.parse(previous):
        raise ValueError("CI candidate must be newer than its baseline")
    tree = ast.parse(source)
    declarations = [node for node in tree.body
                    if isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "__version__"]
    # A second code occurrence could rebind through an import, function, or
    # nested scope. This declaration file needs no such references; comments
    # and strings are still allowed and are never interpreted as declarations.
    identifiers = [token for token in tokenize.generate_tokens(io.StringIO(source).readline)
                   if token.type == tokenize.NAME and token.string == "__version__"]
    if len(declarations) != 1 or len(identifiers) != 1:
        raise ValueError("Expected one top-level backend version declaration")
    value = declarations[0].value
    if not isinstance(value, ast.Constant) or type(value.value) is not str:
        raise ValueError("Backend version declaration must be a string literal")
    if value.value != previous:
        raise ValueError("Previous backend source version mismatch")
    # AST columns are UTF-8 byte offsets, including when comments or another
    # statement contain non-ASCII text. Do not replace matching comments,
    # documentation, or other version-shaped strings anywhere in the file.
    raw = source.encode("utf-8")
    lines = raw.splitlines(keepends=True)
    start = sum(map(len, lines[:value.lineno - 1])) + value.col_offset
    end = sum(map(len, lines[:value.end_lineno - 1])) + value.end_col_offset
    return (raw[:start] + json.dumps(candidate).encode("ascii") + raw[end:]).decode("utf-8")
