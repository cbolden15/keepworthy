import re

_TS_LINE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
_INLINE_TS = re.compile(r"<\d{2}:\d{2}:\d{2}\.\d{3}>")
_TAGS = re.compile(r"</?c[^>]*>")

def clean_vtt(vtt_text: str) -> str:
    lines = []
    for raw in vtt_text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT":
            continue
        if line.startswith(("Kind:", "Language:")):
            continue
        if _TS_LINE.match(line):
            continue
        line = _INLINE_TS.sub("", line)
        line = _TAGS.sub("", line).strip()
        if not line:
            continue
        # collapse rolling duplicates: skip if identical to the last kept line
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return "\n".join(lines)
