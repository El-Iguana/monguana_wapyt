"""
Pipeline text <-> a list of stages, for the stage-list editor. **Both sides.**

The raw pipeline text stays the source of truth (it is what Run sends); the
stage list is another way of editing it. ``split`` turns text into stages and
``join`` turns stages back into text. Pure: no ``bson`` in the browser, so this
never *parses* values. It only finds where each stage starts and ends, which
means skipping over strings, regex literals and comments correctly: a ``,`` or
``]`` inside ``"a, b"``, ``/[a-z]/`` or ``/* … */`` is not structure.

**Disabled stages are block comments**, ``/* off: {$limit: 5} */``. The
server's parser ignores comments, so a disabled stage can never run, and it
survives the round trip between the two views.
"""
from __future__ import annotations

import re

__all__ = ["PipelineTextError", "split", "join", "compose"]


class PipelineTextError(ValueError):
    """Text that cannot be split into stages; the message says where."""


_OFF = re.compile(r"^\s*off:\s*(.*?)\s*$", re.S)
_KEY = re.compile(r"""\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[A-Za-z_$][\w$.]*)\s*:""", re.S)
# After these, a "/" starts a regex literal rather than being division.
_REGEX_AFTER = set("([{,:=!&|?;")


def _scan(text: str, start: int, stop_at_depth_zero: str):
    """
    Walk ``text`` from ``start``, yielding ``(index, marker, depth, end)`` per
    token: ``marker`` is the character for code, the quote for a string,
    ``"/"`` for a regex literal, ``"/*"`` or ``"//"`` for a comment; ``end``
    is where the token stops. ``depth`` is the nesting after the token. The
    unmatched ``stop_at_depth_zero`` closer is yielded with depth ``-1`` and
    ends the walk.
    """
    depth = 0
    i = start
    previous = "["  # the significant character before i
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise PipelineTextError(_where(text, i, "Unterminated /* comment"))
            yield (i, "/*", depth, end + 2)
            i = end + 2
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            yield (i, "//", depth, end)
            i = end
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n":
                    raise PipelineTextError(_where(text, i, "Unterminated string"))
                j += 1
            if j >= n:
                raise PipelineTextError(_where(text, i, "Unterminated string"))
            yield (i, ch, depth, j + 1)
            previous = ch
            i = j + 1
            continue
        if ch == "/" and previous in _REGEX_AFTER:
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            if j >= n or text[j] != "/":
                raise PipelineTextError(_where(text, i, "Unterminated regular expression"))
            j += 1
            while j < n and text[j] in "imxslu":
                j += 1
            yield (i, "/", depth, j)
            previous = "/"
            i = j
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            if depth == 0:
                if ch == stop_at_depth_zero:
                    yield (i, ch, -1, i + 1)
                    return
                raise PipelineTextError(_where(text, i, f"Unexpected {ch!r}"))
            depth -= 1
        yield (i, ch, depth, i + 1)
        previous = ch
        i += 1
    raise PipelineTextError(_where(text, n, f"Missing {stop_at_depth_zero!r}"))


def _where(text: str, index: int, message: str) -> str:
    line = text.count("\n", 0, index) + 1
    column = index - (text.rfind("\n", 0, index) + 1) + 1
    return f"{message} (line {line}, column {column})"


def _stage(text: str, offset: int, enabled: bool) -> dict:
    """``{$op: body}`` -> ``{"op", "body", "enabled"}``."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        raise PipelineTextError(_where(text, offset, "A stage must be an object like {$match: {…}}"))
    inner = stripped[1:-1]
    match = _KEY.match(inner)
    if not match:
        raise PipelineTextError(_where(text, offset, "A stage must start with its name, like $match"))
    op = match.group(1)
    if op[0] in "\"'":
        op = op[1:-1]
    body_start = match.end()
    # The body runs to the end, unless a top-level comma begins a second key.
    body_end = len(inner)
    for index, ch, depth, _end in _scan(inner + "}", body_start, "}"):
        if depth == -1:
            break
        if ch == "," and depth == 0:
            rest = inner[index + 1:].strip()
            if rest:
                raise PipelineTextError(_where(text, offset, f"Stage {op} has more than one key"))
            body_end = index
            break
    body = inner[body_start:body_end].strip()
    if not body:
        raise PipelineTextError(_where(text, offset, f"Stage {op} has no value"))
    return {"op": op, "body": body, "enabled": enabled}


def split(text: str) -> list:
    """
    Stages from pipeline text: ``[{…}, {…}]``, or one bare ``{…}`` stage.
    Top-level ``/* off: … */`` comments come back as disabled stages; other
    top-level comments are dropped.
    """
    source = text or ""
    start = len(source) - len(source.lstrip())
    if start >= len(source):
        return []
    if source[start] == "{":
        return [_stage(source[start:], start, True)]
    if source[start] != "[":
        raise PipelineTextError(_where(source, start, "A pipeline is an array: [{$match: …}, …]"))

    stages: list = []
    element_start = None
    element_end = None  # after the element's last code token, not a comment
    for index, marker, depth, end in _scan(source, start + 1, "]"):
        if depth == -1:  # the closing bracket
            if element_start is not None:
                stages.append(_stage(source[element_start:element_end], element_start, True))
            trailing = source[end:].strip()
            if trailing and not trailing.startswith(("//", "/*")):
                raise PipelineTextError(_where(source, end, "Unexpected text after the pipeline"))
            return stages
        if depth == 0 and marker == "/*" and element_start is None:
            comment = _OFF.match(source[index + 2:end - 2])
            if comment:
                stages.append(_stage(comment.group(1), index + 2, False))
            continue
        if depth == 0 and marker == "//" and element_start is None:
            continue
        if depth == 0 and marker == ",":
            if element_start is None:
                raise PipelineTextError(_where(source, index, "Empty stage"))
            stages.append(_stage(source[element_start:element_end], element_start, True))
            element_start = None
            continue
        if marker in ("/*", "//"):
            continue  # a comment inside or after an element is not its end
        if element_start is None:
            element_start = index
        element_end = end
    return stages  # unreachable: _scan raises on a missing "]"


def _indent(body: str, prefix: str) -> str:
    return body.replace("\n", "\n" + prefix)


def join(stages) -> str:
    """Stages -> pipeline text, disabled ones as ``/* off: … */`` comments."""
    lines = []
    for stage in stages or []:
        body = (stage.get("body") or "").strip() or "{}"
        entry = "{" + stage["op"] + ": " + _indent(body, "  ") + "}"
        if stage.get("enabled", True):
            lines.append("  " + entry + ",")
        else:
            if "*/" in entry:
                raise PipelineTextError(
                    f"Stage {stage['op']} contains */ and cannot be disabled; remove it instead"
                )
            lines.append("  /* off: " + entry + " */")
    if not lines:
        return "[]"
    # No comma after the last enabled stage (trailing commas parse, but read
    # as a mistake).
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].endswith(","):
            lines[index] = lines[index][:-1]
            break
    return "[\n" + "\n".join(lines) + "\n]"


def compose(stages, upto: int | None = None) -> str:
    """The enabled stages (up to and including index ``upto``) as runnable text."""
    chosen = list(stages or [])
    if upto is not None:
        chosen = chosen[: upto + 1]
    return join([stage for stage in chosen if stage.get("enabled", True)])
