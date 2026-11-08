"""Reader-friendly display of LaTeX fragments (display only — sources are never changed).

* Zero-argument user macros (``\\newcommand{\\x}{31.2}``, ``\\def\\x{...}``, ``\\renewcommand``,
  ``\\providecommand``, ``\\DeclareRobustCommand``) are expanded, recursively, e.g. numbers
  generated into ``numbers.tex``.
* Common typography is rendered: ``\\,`` → thin space, ``~`` → no-break space, ``--`` → –,
  ``---`` → —, ``\\%`` → %, ``\\cite{a,b}`` → [a, b], ``\\ref{x}`` → ‹x›, ``\\num{x}`` / ``\\SI{x}{u}``.
* Inline math that is only a number after expansion loses its ``$``.
"""
from __future__ import annotations

import ast
import math
import operator
import re

DEFINERS = ("newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "def")
_DEFINER = re.compile(r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|def)(\*?)(?![A-Za-z@])")
# Pairs are consumed left to right, so "$a$, $b$" never pairs the inner "$, $".
_INLINE_MATH = re.compile(r"\$((?:[^$\\]|\\.){1,80}?)\$")
_SUPERSCRIPT = str.maketrans("0123456789+-()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾")
_SUBSCRIPT = str.maketrans("0123456789+-()", "₀₁₂₃₄₅₆₇₈₉₊₋₍₎")


def _simple_math(match):
    """Inline math without commands: digit super/subscripts → Unicode, ``_{tiny}`` → ``_tiny``,
    then drop the ``$``. Anything else (e.g. ``$x_1 + y$`` stays) is left as written."""
    body = match[1]
    if "\\" in body:
        return match[0]
    body = re.sub(r"\^\{?([0-9+\-()]{1,6})\}?", lambda m: m[1].translate(_SUPERSCRIPT), body)
    body = re.sub(r"_\{?([0-9+\-()]{1,6})\}?(?![A-Za-z])", lambda m: m[1].translate(_SUBSCRIPT), body)
    body = re.sub(r"(?<![A-Za-z0-9])_\{?([A-Za-z]{2,20})\}?", r"_\1", body)
    if re.search(r"[\^{}]", body) or re.search(r"[A-Za-z]_|_[A-Za-z]\b(?<!_[A-Za-z]{2})", body):
        return match[0]
    return body.strip()
MAX_DEPTH = 8
MAX_BODY = 400


def _strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", text)


def _group(text: str, start: int) -> int | None:
    """Index just after the balanced {...} starting at ``start`` (which must be '{')."""
    if start >= len(text) or text[start] != "{":
        return None
    depth, i = 0, start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def collect_macros(texts) -> dict[str, str]:
    """Zero-argument macro definitions found in the given TeX sources (later ones win, except
    \\providecommand which never overrides)."""
    macros: dict[str, str] = {}
    for raw in texts:
        text = _strip_comments(raw)
        for match in _DEFINER.finditer(text):
            kind, i = match[1], match.end()
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
            if text.startswith("{", i):
                close = _group(text, i)
                inner = text[i + 1:close - 1].strip() if close else ""
                name_match = re.fullmatch(r"\\([A-Za-z@]+)", inner)
                if not name_match:
                    continue
                name, i = name_match[1], close
            else:
                name_match = re.match(r"\\([A-Za-z@]+)", text[i:])
                if not name_match:
                    continue
                name, i = name_match[1], i + name_match.end()
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
            if kind == "def":
                if text.startswith("#", i):
                    continue  # \def\x#1{...}: takes arguments
            elif text.startswith("[", i):
                arity = re.match(r"\[\s*(\d)\s*\]", text[i:])
                if not arity or arity[1] != "0":
                    continue  # takes arguments (or malformed)
                i += arity.end()
                while i < len(text) and text[i] in " \t\r\n":
                    i += 1
            close = _group(text, i)
            if close is None:
                continue
            body = text[i + 1:close - 1].strip()
            if len(body) > MAX_BODY or (kind == "providecommand" and name in macros):
                continue
            macros[name] = body
    return macros


_MACRO_USE = re.compile(r"\\([A-Za-z@]+)(?![A-Za-z@])(\{\})?([ \t]*)")


def _expand(text: str, macros: dict[str, str], depth: int = 0) -> str:
    """Expand known macros like TeX: spaces after a control word are swallowed (an empty {} too),
    unless the definition ends with \\xspace."""
    if depth >= MAX_DEPTH or not macros:
        return text

    def replace(match):
        name, empty, spaces = match[1], match[2], match[3]
        if name not in macros:
            return match[0]
        body = macros[name]
        keep_space = bool(spaces) and (body.rstrip().endswith("\\xspace") or bool(empty))
        return _expand(body, macros, depth + 1) + (" " if keep_space else "")
    return _MACRO_USE.sub(replace, text)


def _arg_command(text: str, names: str, render) -> str:
    """Replace \\name[opt]{a}{b}... occurrences using ``render(args)``."""
    pattern = re.compile(r"\\(" + names + r")\*?(?:\[[^\]]*\])*\s*(?=\{)")
    out, last = [], 0
    for match in pattern.finditer(text):
        if match.start() < last:
            continue
        args, i = [], match.end()
        while i < len(text) and text[i] == "{" and len(args) < 2:
            close = _group(text, i)
            if close is None:
                break
            args.append(text[i + 1:close - 1])
            i = close
        if not args:
            continue
        rendered = render(match[1], args)
        if rendered is None:
            continue
        out.append(text[last:match.start()])
        out.append(rendered)
        last = i
    out.append(text[last:])
    return "".join(out)


_ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
               ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _round(value, digits=0):
    """xfp rounds half away from zero (Python's round() is banker's rounding)."""
    scale = 10 ** digits
    return math.floor(abs(value) * scale + 0.5) / scale * (1 if value >= 0 else -1)


_FUNCTIONS = {"round": _round, "abs": abs, "min": min, "max": max, "sqrt": math.sqrt,
              "trunc": math.trunc, "floor": math.floor, "ceil": math.ceil}


def _evaluate(expression: str):
    """\\fpeval for plain arithmetic only (numbers, + - * / ^, parentheses and the xfp functions in
    _FUNCTIONS); anything else → None. Parsed with ast and walked by hand — never eval()."""
    expression = expression.replace("^", "**").replace("−", "-").strip()
    if not expression or len(expression) > 200 or not re.fullmatch(r"[\d\s.+\-*/(),a-z]+", expression):
        return None

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS
                and not node.keywords and 1 <= len(node.args) <= 3):
            values = [walk(a) for a in node.args]
            if node.func.id == "round" and len(values) == 2:
                values[1] = int(values[1])
            return _FUNCTIONS[node.func.id](*values)
        if isinstance(node, ast.BinOp) and type(node.op) in _ARITHMETIC:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError
            return _ARITHMETIC[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITHMETIC:
            return _ARITHMETIC[type(node.op)](walk(node.operand))
        raise ValueError
    try:
        value = walk(ast.parse(expression, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
        return None
    text = f"{value:.12g}"
    return "0" if text == "-0" else text


def _render_args(name: str, args: list[str]):
    if name == "fpeval":
        return _evaluate(args[0])
    if name == "label":
        return ""
    if name in {"cite", "citep", "citet", "citealp", "citeauthor", "citeyear", "parencite", "textcite", "autocite"}:
        return "[" + ", ".join(k.strip() for k in args[0].split(",") if k.strip()) + "]"
    if name in {"ref", "eqref", "autoref", "cref", "Cref", "pageref"}:
        return "‹" + args[0].strip() + "›"
    if name in {"num", "ensuremath", "text", "textrm", "mathrm", "mbox", "textnormal"}:
        return args[0]
    if name in {"SI", "qty"} and len(args) == 2:
        unit = re.sub(r"\\([A-Za-z]+)", lambda m: {"percent": "%", "decibel": "dB", "second": "s",
                                                      "milli": "m", "kilo": "k", "mega": "M", "per": "/"}.get(m[1], m[1]), args[1])
        return f"{args[0]}\u2009{unit.replace(' ', '')}"
    return None


def prettify(text: str, macros: dict[str, str]) -> str:
    """Idempotent, display-only rendering of a string that may contain raw LaTeX."""
    if not text or ("\\" not in text and "~" not in text and "--" not in text and "$" not in text):
        return text
    text = _expand(text, macros)
    text = _arg_command(text, r"cite|citep|citet|citealp|citeauthor|citeyear|parencite|textcite|autocite|"
                              r"ref|eqref|autoref|cref|Cref|pageref|num|ensuremath|text|textrm|mathrm|mbox|textnormal|SI|qty|fpeval|label",
                        _render_args)
    text = text.replace("\\xspace", "").replace("\\,", "\u2009").replace("\\;", " ").replace("\\:", " ")
    text = text.replace("\\ ", " ").replace("\\%", "%").replace("\\&", "&").replace("\\#", "#").replace("\\_", "_")
    text = re.sub(r"(?<!\\)~", "\u00a0", text)
    text = text.replace("---", "—").replace("--", "–")
    text = _INLINE_MATH.sub(_simple_math, text)
    text = re.sub("[ \\t]*\u2009[ \\t]*", "\u2009", text)
    # numeric ranges: "31.2 -- 33.5" / "31.2-- 33.5" → "31.2–33.5"
    return re.sub(r"(?<=\d)[ \t\u00a0]*–[ \t\u00a0]*(?=[\d+\-−])", "–", text)
