from __future__ import annotations

import sys


def main() -> None:
    print("fake agent ready", flush=True)
    for line in sys.stdin:
        text = line.strip()
        if text == "approval":
            print("Do you want to proceed? [y/N]", flush=True)
        elif text.lower() in {"y", "yes"}:
            print("approved", flush=True)
        elif text.lower() in {"n", "no"}:
            print("rejected", flush=True)
        else:
            print(f"echo: {text}", flush=True)


if __name__ == "__main__":
    main()

