#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    print("numbered line agent ready", flush=True)
    for raw_line in sys.stdin:
        text = raw_line.strip()
        if not text:
            continue
        if "numbered-lines" in text:
            count, delay = _parse_numbered_lines(text[text.index("numbered-lines") :])
            for index in range(1, count + 1):
                print(f"LINE {index:03d}", flush=True)
                time.sleep(delay)
        else:
            print(f"echo: {text}", flush=True)
    return 0


def _parse_numbered_lines(text: str) -> tuple[int, float]:
    parser = argparse.ArgumentParser(prog="numbered-lines", add_help=False)
    parser.add_argument("command")
    parser.add_argument("count", type=int, nargs="?", default=150)
    parser.add_argument("delay", type=float, nargs="?", default=0.05)
    try:
        args = parser.parse_args(text.split())
    except SystemExit:
        return 150, 0.05
    return max(1, args.count), max(0.0, args.delay)


if __name__ == "__main__":
    raise SystemExit(main())
