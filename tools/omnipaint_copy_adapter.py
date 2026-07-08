#!/usr/bin/env python3
"""Proof adapter for the OmniPaintRemove node (Phase 1).

Copies the source PNG to the output path verbatim so the render/subprocess/
import plumbing is testable end-to-end without the OmniPaint model.

Backend contract (shared with the real adapter to come in Phase 2):

    {python} omnipaint_copy_adapter.py --source SRC --mask MASK --output OUT

Mask semantics: white = remove, black = keep; adapter reads alpha if the mask
PNG has a varying alpha channel, else luma. This stub ignores the mask and
just copies the source, so success = exit 0 and OUT exists.
"""

from __future__ import annotations

import argparse
import shutil
import sys


def main() -> int:
    p = argparse.ArgumentParser(description="OmniPaint proof adapter (copies source to output).")
    p.add_argument("--source", required=True)
    p.add_argument("--mask", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    shutil.copyfile(a.source, a.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
