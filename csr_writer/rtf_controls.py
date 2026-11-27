"""Decode scoped RTF Unicode escapes and discard non-body destinations."""
import re

from .errors import RTFParseError

TOKEN = re.compile(r"\\'[0-9A-Fa-f]{2}|\\[A-Za-z]+-?\d* ?|\\[\s\S]|[{}]|[^{}\\]")
SKIP = re.compile(r"{\\(?:fonttbl|colortbl|stylesheet|info|header[a-z]*|footer[a-z]*|pict|object)\b|{\\\*")
UNICODE = re.compile(r"\\u(-?\d+) ?$")
FALLBACK = re.compile(r"\\uc(\d+) ?$")


def body_controls(raw):
    output = []
    scope = []
    fallback_size = 1
    skipping = False
    remaining = 0
    for m in TOKEN.finditer(raw):
        t = m.group()
        if t == "{":
            scope.append((fallback_size, skipping))
            skipping = skipping or bool(SKIP.match(raw, m.start()))
            remaining = 0
            if not skipping:
                output.append(t)
        elif t == "}":
            if not scope:
                raise RTFParseError("Unbalanced RTF closing group")
            if not skipping:
                output.append(t)
            fallback_size, skipping = scope.pop()
            remaining = 0
        elif not skipping:
            if remaining:
                if t not in {"\r", "\n"}:
                    remaining -= 1
                continue
            uc = FALLBACK.fullmatch(t)
            u = UNICODE.fullmatch(t)
            if uc:
                fallback_size = int(uc[1])
            elif u:
                number = int(u[1])
                if not -32768 <= number <= 65535:
                    raise RTFParseError("Invalid RTF Unicode code unit")
                char = chr(number % 65536)
                # Preserve literal syntax characters through the legacy control stripper.
                output.append({"{": "\ufdd0", "}": "\ufdd1", "\\": "\ufdd2"}.get(char, char))
                remaining = fallback_size
            elif t.startswith("\\bin") and re.match(r"\\bin\d", t):
                raise RTFParseError("Binary RTF body is not supported")
            else:
                output.append(t)
    if scope:
        raise RTFParseError("Unclosed RTF group")
    try:
        return "".join(output).encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError as exc:
        raise RTFParseError("Incomplete RTF Unicode surrogate pair") from exc
