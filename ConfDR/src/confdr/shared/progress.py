"""Flush formalization progress to stderr without exposing prompts or credentials."""

import sys
from datetime import datetime


def progress(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", file=sys.stderr, flush=True)
