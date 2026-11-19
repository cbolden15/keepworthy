import re
from itertools import groupby

_TIMESTAMP = r"(?:[0-9]{2,}:)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3}"
_TS_LINE = re.compile(rf"^{_TIMESTAMP}[ \t]+-->[ \t]+{_TIMESTAMP}(?:[ \t]|$)")
_INLINE_TS = re.compile(rf"<{_TIMESTAMP}>")
_TAGS = re.compile(r"</?c[^>]*>")

def clean_vtt(vtt_text: str) -> str:
    """Extract transcript text from timed WebVTT cues."""
    lines = []
    for has_text, block in groupby(vtt_text.splitlines(), key=lambda line: bool(line.strip())):
        if not has_text:
            continue
        first = next(block).strip()
        if first.split(maxsplit=1)[0] in {"WEBVTT", "NOTE", "STYLE", "REGION"}:
            continue
        # An optional cue identifier can precede the timing line.
        if not _TS_LINE.match(first) and not _TS_LINE.match(next(block, "").strip()):
            continue
        for raw in block:
            line = _TAGS.sub("", _INLINE_TS.sub("", raw)).strip()
            if line and (not lines or lines[-1] != line):
                lines.append(line)
    return "\n".join(lines)
