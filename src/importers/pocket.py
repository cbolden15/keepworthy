"""Pocket list.csv export. Columns per the published export:
title,url,time_added,tags,status. Tags are separated by "|"."""
from src.importers import MAX_BOOKMARKS, Collector, ParseResult, csv_rows, header_names


def sniff(head: bytes) -> bool:
    """True when the header line contains both "url" and "time_added"."""
    return {"url", "time_added"} <= header_names(head)


def parse(path, *, max_items: int = MAX_BOOKMARKS) -> ParseResult:
    collector = Collector("pocket", max_items)
    for row in csv_rows(path, required=("url", "time_added"), label="Pocket"):
        # status (unread or archive) is read and ignored: both are kept
        collector.add(row.get("url"), title=row.get("title") or "",
                      tags=(row.get("tags") or "").split("|"),
                      saved_at=row.get("time_added") or "")
    return collector.result()
