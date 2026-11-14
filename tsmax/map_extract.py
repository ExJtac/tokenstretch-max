"""Deterministic, bounded source extraction for the local project map.

Python uses its parser. JS/TS uses a small lexer, not a compiler: comments and
literal bodies are skipped, and declaration recognition is explicitly approximate.
"""
from __future__ import annotations

import ast
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_SYMBOLS = 2000
MAX_IMPORTS = 1000
MAX_TOKENS = 100_000
MAX_SEARCH_CHARS = 65_536


def one_line(value: str, limit: int = 160) -> str:
    return "".join(char for char in " ".join(value.split()) if not unicodedata.category(char).startswith("C"))[:limit]


def _summary(path: str, language: str, symbols: list[dict], doc: str = "") -> str:
    if doc:
        return one_line(doc.splitlines()[0])
    names = ", ".join(item["name"] for item in symbols[:4])
    return one_line(f"{language}: {names}" if names else f"{language} file {PurePosixPath(path).name}")


def _python(path: str, text: str) -> dict:
    tree = ast.parse(text, filename=path)
    symbols: list[dict] = []
    imports: list[dict] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []

        def declaration(self, node, kind):
            if len(symbols) < MAX_SYMBOLS:
                symbols.append({"name": ".".join([*self.scope, node.name]), "kind": kind, "line": node.lineno})
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.declaration(node, "function")

        def visit_AsyncFunctionDef(self, node):
            self.declaration(node, "async function")

        def visit_ClassDef(self, node):
            self.declaration(node, "class")

        def visit_Import(self, node):
            for alias in node.names:
                if len(imports) < MAX_IMPORTS:
                    imports.append({"name": alias.name, "line": node.lineno})

        def visit_ImportFrom(self, node):
            prefix = "." * node.level + (node.module or "")
            for alias in node.names:
                if len(imports) < MAX_IMPORTS:
                    imports.append({"name": prefix + ("." if node.module else "") + alias.name, "line": node.lineno})

    Visitor().visit(tree)
    return {"language": "python", "approximate": False, "symbols": symbols, "imports": imports,
            "summary": _summary(path, "Python", symbols, ast.get_docstring(tree) or ""),
            "limits": ["symbol/import cap"] if len(symbols) == MAX_SYMBOLS or len(imports) == MAX_IMPORTS else []}


@dataclass(frozen=True)
class Token:
    value: str
    line: int
    kind: str = "punct"


_WORD = re.compile(r"[A-Za-z_$][\w$]*")


def _lex(text: str) -> tuple[list[Token], bool]:
    """Skip comments, regex bodies, and template literals without executing code."""
    tokens: list[Token] = []
    pos, line, size = 0, 1, len(text)
    while pos < size and len(tokens) < MAX_TOKENS:
        char = text[pos]
        if char.isspace():
            line += char == "\n"
            pos += 1
            continue
        if text.startswith("//", pos):
            end = text.find("\n", pos + 2)
            pos = size if end < 0 else end
            continue
        if text.startswith("/*", pos):
            end = text.find("*/", pos + 2)
            end = size if end < 0 else end + 2
            line += text[pos:end].count("\n")
            pos = end
            continue
        if char in "'\"`":
            start, first_line, quote = pos, line, char
            pos += 1
            while pos < size:
                if text[pos] == "\\":
                    line += pos + 1 < size and text[pos + 1] == "\n"
                    pos += 2
                elif text[pos] == quote:
                    pos += 1
                    break
                else:
                    line += text[pos] == "\n"
                    pos += 1
            # Template expressions are deliberately not parsed as declarations.
            tokens.append(Token(text[start + 1:max(start + 1, pos - 1)][:500] if quote != "`" else "", first_line, "string"))
            continue
        # Regex literals occur where an expression may begin. This is lexical,
        # so ambiguous division/regex syntax remains a documented limitation.
        if char == "/" and (not tokens or tokens[-1].value in {"=", "(", "[", ",", ":", "return", "=>", "!", "?", ";", "{"}):
            pos += 1
            in_class = False
            while pos < size and text[pos] != "\n":
                if text[pos] == "\\":
                    pos += 2
                    continue
                if text[pos] == "[":
                    in_class = True
                elif text[pos] == "]":
                    in_class = False
                elif text[pos] == "/" and not in_class:
                    pos += 1
                    while pos < size and text[pos].isalpha():
                        pos += 1
                    break
                pos += 1
            tokens.append(Token("", line, "literal"))
            continue
        match = _WORD.match(text, pos)
        if match:
            tokens.append(Token(match.group(), line, "word"))
            pos = match.end()
        else:
            op = "=>" if text.startswith("=>", pos) else char
            tokens.append(Token(op, line))
            pos += len(op)
    return tokens, pos < size


def _javascript(path: str, text: str) -> dict:
    tokens, limited = _lex(text)
    symbols: list[dict] = []
    imports: list[dict] = []
    seen: set[tuple] = set()
    language = "typescript" if PurePosixPath(path).suffix.lower() in {".ts", ".tsx", ".mts", ".cts"} else "javascript"

    def add(token, kind):
        key = (token.value, token.line, kind)
        if token.kind == "word" and key not in seen and len(symbols) < MAX_SYMBOLS:
            seen.add(key)
            symbols.append({"name": token.value, "kind": kind, "line": token.line})

    for i, token in enumerate(tokens):
        if token.kind != "word":
            continue
        word = token.value
        after = tokens[i + 1:i + 202]
        if word in {"function", "class", "interface", "type", "enum"}:
            if after and after[0].value == "*":
                after = after[1:]
            if after:
                add(after[0], word)
        elif word in {"const", "let", "var"} and len(after) > 1 and after[0].kind == "word":
            # Find an assignment then an arrow/function before a statement ends.
            # A call that takes an arrow callback may be over-classified here.
            assigned = False
            for part in after[1:]:
                if part.value == ";" or part.line > token.line + 12:
                    break
                if part.value == "=":
                    assigned = True
                if assigned and part.value in {"=>", "function"}:
                    add(after[0], "function")
                    break
        if word in {"import", "export"}:
            # import "x"; import {...} from "x"; export {...} from "x";
            # import("x") is included, without interpreting the string itself.
            for j, part in enumerate(after):
                if part.value == ";" or part.line > token.line + 12:
                    break
                if part.kind == "string" and (word == "import" or (j and after[j - 1].value == "from")):
                    if len(imports) < MAX_IMPORTS:
                        imports.append({"name": one_line(part.value, 300), "line": token.line})
                    break
        elif word == "require" and len(after) > 1 and after[0].value == "(" and after[1].kind == "string":
            if len(imports) < MAX_IMPORTS:
                imports.append({"name": one_line(after[1].value, 300), "line": token.line})
    return {"language": language, "approximate": True, "symbols": symbols, "imports": imports,
            "summary": _summary(path, language, symbols),
            "limits": ["JS/TS lexical extraction (not a compiler; template expressions, complex methods and callback assignments may be missed or approximate)"]
                      + (["token/symbol/import cap"] if limited or len(symbols) == MAX_SYMBOLS or len(imports) == MAX_IMPORTS else [])}


def extract(path: str, text: str) -> dict:
    """Return JSON-serializable extraction. Malformed source stays searchable."""
    suffix = PurePosixPath(path).suffix.lower()
    try:
        if suffix in {".py", ".pyi"}:
            result = _python(path, text)
        elif suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
            result = _javascript(path, text)
        else:
            result = {"language": suffix.lstrip(".") or "text", "approximate": True,
                      "symbols": [], "imports": [],
                      "summary": one_line(next((line for line in text.splitlines() if line.strip()), "")) or PurePosixPath(path).name,
                      "limits": ["text fallback (no language parser)"]}
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        result = {"language": "python" if suffix in {".py", ".pyi"} else "text", "approximate": True,
                  "symbols": [], "imports": [], "summary": f"{PurePosixPath(path).name}: source could not be parsed",
                  "limits": ["parse failed; text fallback"]}
    for item in [*result["symbols"], *result["imports"]]:
        if len(item["name"]) > 500:
            item["name"] = one_line(item["name"], 500)
            result["approximate"] = True
            if "symbol/import names truncated" not in result["limits"]:
                result["limits"].append("symbol/import names truncated")
    result["text"] = text[:MAX_SEARCH_CHARS]
    if len(text) > MAX_SEARCH_CHARS:
        result["limits"].append("search text truncated")
    return result
