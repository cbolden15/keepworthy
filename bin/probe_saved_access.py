#!/usr/bin/env python3
"""Read saved feeds over HTTP; private output stays outside Git."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.saved_feeds import Client, connection, documents, main, private_json, time

if __name__ == "__main__":
    sys.exit(main())
