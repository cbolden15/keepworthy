#!/usr/bin/env python3
"""One-time backfill of `processed_at:` into notes written before the field existed.

Every existing note carries only a date-only `processed:`, so the real time it
was written is gone -- nothing in the vault or in state.json recorded it, and
file mtime is untrustworthy here because the vault is Syncthing-synced across
three devices and mtime can reflect a sync rather than a write.

So the value stamped is midnight of the note's own `processed:` date. That is
not a guess dressed up as data: it sorts old notes by date and ties them within
a day, which is exactly what they do today. New notes get real times from the
pipeline and sort properly among themselves.

Safety, matching migrate_notes.py: dry-run by default, idempotent, never
rewrites an existing `processed_at:`, never touches `processed:`, never
reorders or reformats anything else in the file, and writes atomically.

    python3 backfill_processed_at.py                  # report only
    python3 backfill_processed_at.py --apply          # write
    python3 backfill_processed_at.py --vault ~/Brain/saved-content --apply
"""
import argparse
import re
import sys
from pathlib import Path

# The frontmatter block is rewritten by line, not parsed and re-emitted: a
# yaml round-trip would restyle quoting and key order across 129 hand-touched
# notes, and the only change wanted here is one inserted line.
_PROCESSED = re.compile(r"^processed:\s*(\d{4}-\d{2}-\d{2})\s*$")
_PROCESSED_AT = re.compile(r"^processed_at:\s*\S")


def frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    """(first, last) indices of the frontmatter body, or None if there is none."""
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return 1, i
    return None


def backfilled(text: str) -> str | None:
    """The note with `processed_at:` inserted, or None if it needs no change."""
    lines = text.splitlines(keepends=True)
    bounds = frontmatter_bounds(lines)
    if bounds is None:
        return None
    first, last = bounds
    at = None
    for i in range(first, last):
        line = lines[i]
        if _PROCESSED_AT.match(line):
            return None  # already stamped; leave it exactly as it is
        if at is None and _PROCESSED.match(line):
            at = i
    if at is None:
        return None  # no date-only `processed:` to derive a timestamp from
    stamp = _PROCESSED.match(lines[at]).group(1)
    newline = "\r\n" if lines[at].endswith("\r\n") else "\n"
    lines.insert(at + 1, f"processed_at: {stamp}T00:00:00{newline}")
    return "".join(lines)


def write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def run(root: Path, apply: bool) -> tuple[int, int, int]:
    changed = skipped = failed = 0
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            print(f"  !! unreadable, skipped: {path.name} ({e})")
            failed += 1
            continue
        out = backfilled(text)
        if out is None:
            skipped += 1
            continue
        changed += 1
        stamp = next(l for l in out.splitlines() if l.startswith("processed_at:"))
        print(f"  {'wrote ' if apply else 'would '} {path.name}  ->  {stamp}")
        if apply:
            try:
                write_atomically(path, out)
            except OSError as e:
                print(f"  !! write failed: {path.name} ({e})")
                changed -= 1
                failed += 1
    return changed, skipped, failed


def default_vault() -> Path | None:
    """VAULT_DIR/CONTENT_ROOT from config.env, when this runs where the
    pipeline runs. On any other machine the path will not exist and --vault
    is required."""
    cfg = Path(__file__).resolve().parent / "config.env"
    if not cfg.exists():
        return None
    values = {}
    for line in cfg.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    vault, content = values.get("VAULT_DIR"), values.get("CONTENT_ROOT")
    return Path(vault, content) if vault and content else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", type=Path, default=None,
                    help="content tree to walk (default: VAULT_DIR/CONTENT_ROOT "
                         "from config.env)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this only reports")
    args = ap.parse_args(argv)

    root = (args.vault or default_vault())
    if root is None:
        print("no --vault given and config.env has no VAULT_DIR/CONTENT_ROOT",
              file=sys.stderr)
        return 2
    root = root.expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}\npass --vault explicitly if the vault "
              f"lives elsewhere on this machine.", file=sys.stderr)
        return 2

    print(f"{'applying to' if args.apply else 'dry run over'} {root}")
    changed, skipped, failed = run(root, args.apply)
    verb = "stamped" if args.apply else "would stamp"
    print(f"\n{verb} {changed}, already-stamped-or-no-processed {skipped}, "
          f"failed {failed}")
    if not args.apply and changed:
        print("re-run with --apply to write.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
