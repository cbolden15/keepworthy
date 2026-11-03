"""Raindrop.io CSV export. Columns per the published export:
id,title,note,excerpt,url,folder,tags,created,cover,highlights,favorite.
Tags are separated by ","; folder keeps its "/" nesting as exported."""
from src.importers import MAX_BOOKMARKS, Collector, ParseResult, csv_rows, header_names


def sniff(head: bytes) -> bool:
    """True when the header contains "url" and "created" and at least one of
    "folder", "note", "excerpt"."""
    names = header_names(head)
    return {"url", "created"} <= names and bool(names & {"folder", "note", "excerpt"})


def parse(path, *, max_items: int = MAX_BOOKMARKS) -> ParseResult:
    collector = Collector("raindrop", max_items)
    for row in csv_rows(path, required=("url", "created"), label="Raindrop"):
        # excerpt and highlights are read and ignored in v1
        collector.add(row.get("url"), title=row.get("title") or "",
                      folder=row.get("folder") or "",
                      tags=(row.get("tags") or "").split(","),
                      note=row.get("note") or "", saved_at=row.get("created") or "")
    return collector.result()
