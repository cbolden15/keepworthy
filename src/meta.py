import re

_DEFAULT = {"category": "other", "tags": [], "topics": [], "topic": "Other",
            "subtopic": "General"}
_META_BLOCK = re.compile(r"```meta\s*\n(.*?)\n```", re.DOTALL)

def _parse_list(val: str) -> list[str]:
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        val = val[1:-1]
    return [item.strip() for item in val.split(",") if item.strip()]

def parse_model_output(text: str) -> tuple[dict, str]:
    m = _META_BLOCK.search(text)
    if not m:
        return dict(_DEFAULT), text.strip()

    meta = {"category": "other", "tags": [], "topics": [], "topic": "Other",
            "subtopic": "General"}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if key == "category":
            meta["category"] = val.strip() or "other"
        elif key == "tags":
            meta["tags"] = _parse_list(val)
        elif key == "topics":
            meta["topics"] = _parse_list(val)
        elif key == "topic":
            meta["topic"] = val.strip() or "Other"
        elif key == "subtopic":
            meta["subtopic"] = val.strip() or "General"

    # body = everything after the meta block, dropping a leading `---` separator
    body = text[m.end():].lstrip()
    if body.startswith("---"):
        body = body[3:].lstrip()
    return meta, body.strip()
