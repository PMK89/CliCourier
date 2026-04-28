#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

TELEGRAM_WEB_URL = "https://web.telegram.org/"
DEFAULT_PROFILE_DIR = ".playwright/telegram-profile"
DEFAULT_REPORT_PATH = "tmp/telegram_web_editing_report.json"
DEFAULT_TEST_COMMAND = "numbered-lines 150 0.05"
LINE_RE = re.compile(r"LINE\s+(\d{3})")


def main() -> int:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print(
            "Playwright is not installed. Run:\n"
            "  uv run --with playwright python -m playwright install chromium\n"
            "  uv run --with playwright python scripts/verify_telegram_web_editing.py",
            file=sys.stderr,
        )
        return 2

    chat_name = os.environ.get("TELEGRAM_BOT_CHAT_NAME", "").strip()
    command = os.environ.get("CLICOURIER_TEST_COMMAND", DEFAULT_TEST_COMMAND)
    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "false").lower() in {"1", "true", "yes"}
    profile_dir = Path(os.environ.get("PLAYWRIGHT_PROFILE_DIR", DEFAULT_PROFILE_DIR))
    report_path = Path(os.environ.get("TELEGRAM_WEB_REPORT_PATH", DEFAULT_REPORT_PATH))
    observe_seconds = float(os.environ.get("TELEGRAM_WEB_OBSERVE_SECONDS", "25"))

    profile_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1360, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(TELEGRAM_WEB_URL, wait_until="domcontentloaded")
        print(f"Opened {TELEGRAM_WEB_URL} using profile {profile_dir}")
        input("Log into Telegram Web if needed, open/confirm the bot chat, then press Enter here.")

        if chat_name:
            try:
                open_chat(page, chat_name, PlaywrightTimeoutError)
            except Exception as exc:  # noqa: BLE001 - Telegram Web DOM changes frequently
                print(f"Automatic chat search failed: {exc}")
                input("Open the bot chat manually in the browser, then press Enter here.")
        else:
            input("TELEGRAM_BOT_CHAT_NAME is not set. Open the bot chat manually, then press Enter here.")

        before = observe_output_candidates(page)
        print(f"Sending test command: {command}")
        send_chat_message(page, command, PlaywrightTimeoutError)
        samples = observe_streaming_output(page, before=before, observe_seconds=observe_seconds)
        report = build_report(samples=samples, command=command, chat_name=chat_name)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], indent=2))
        print(f"Wrote report: {report_path}")
        context.close()

    return 0 if report["summary"]["passed"] else 1


def open_chat(page, chat_name: str, timeout_error_type: type[Exception]) -> None:
    page.keyboard.press("Control+K")
    page.wait_for_timeout(500)
    active = page.locator("[contenteditable='true'], input[type='text']").first
    active.click(timeout=5000)
    page.keyboard.insert_text(chat_name)
    page.wait_for_timeout(1500)
    page.get_by_text(chat_name, exact=False).first.click(timeout=8000)
    page.wait_for_timeout(1000)


def send_chat_message(page, text: str, timeout_error_type: type[Exception]) -> None:
    selectors = [
        "div[contenteditable='true'][role='textbox']",
        "div.input-message-input[contenteditable='true']",
        "[contenteditable='true']",
    ]
    last_error: Exception | None = None
    for selector in selectors:
        try:
            box = page.locator(selector).last
            box.click(timeout=5000)
            page.keyboard.insert_text(text)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1000)
            return
        except timeout_error_type as exc:
            last_error = exc
    raise RuntimeError(f"Could not find Telegram message composer: {last_error}")


def observe_streaming_output(page, *, before: list[dict[str, Any]], observe_seconds: float) -> list[dict[str, Any]]:
    before_texts = {candidate["text"] for candidate in before}
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + observe_seconds
    while time.monotonic() < deadline:
        candidates = [
            candidate
            for candidate in observe_output_candidates(page)
            if candidate["text"] not in before_texts
        ]
        best = candidates[0] if candidates else None
        if best is not None:
            lines = LINE_RE.findall(best["text"])
            sample = {
                "elapsed_seconds": round(observe_seconds - (deadline - time.monotonic()), 2),
                "text": best["text"],
                "char_count": len(best["text"]),
                "line_numbers": lines,
                "line_count": len(lines),
                "distinct_candidate_count": len({candidate["text"] for candidate in candidates}),
            }
            if not samples or sample["text"] != samples[-1]["text"]:
                print(
                    "Observed output: "
                    f"{sample['line_count']} LINE entries, "
                    f"{sample['char_count']} chars, "
                    f"last={lines[-1] if lines else '-'}"
                )
                samples.append(sample)
            if "Finished." in best["text"] and "LINE 150" in best["text"]:
                break
        page.wait_for_timeout(1000)
    return samples


def observe_output_candidates(page) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
          const nodes = Array.from(document.querySelectorAll('div, message, section, article'));
          const candidates = [];
          for (const [index, node] of nodes.entries()) {
            const text = (node.innerText || '').trim();
            if (!text.includes('LINE ') || !/Showing (latest|final) 60 lines/.test(text)) continue;
            candidates.push({ index, text, length: text.length });
          }
          const byText = new Map();
          for (const candidate of candidates.sort((a, b) => a.length - b.length)) {
            if (!byText.has(candidate.text)) byText.set(candidate.text, candidate);
          }
          return Array.from(byText.values()).sort((a, b) => a.length - b.length).slice(0, 10);
        }
        """
    )


def build_report(*, samples: list[dict[str, Any]], command: str, chat_name: str) -> dict[str, Any]:
    final = samples[-1] if samples else {"text": "", "line_numbers": [], "char_count": 0, "line_count": 0}
    final_lines = final.get("line_numbers", [])
    final_set = set(final_lines)
    expected = {f"{index:03d}" for index in range(91, 151)}
    max_line_count = max((sample["line_count"] for sample in samples), default=0)
    max_chars = max((sample["char_count"] for sample in samples), default=0)
    distinct_texts = len({sample["text"] for sample in samples})
    final_has_expected_tail = expected.issubset(final_set)
    summary = {
        "passed": bool(
            samples
            and distinct_texts >= 2
            and max_line_count <= 60
            and max_chars < 4096
            and final_has_expected_tail
            and "001" not in final_set
        ),
        "samples": len(samples),
        "distinct_texts": distinct_texts,
        "max_line_count": max_line_count,
        "max_chars": max_chars,
        "final_has_lines_091_to_150": final_has_expected_tail,
        "final_contains_line_001": "001" in final_set,
    }
    return {
        "command": command,
        "chat_name": chat_name,
        "summary": summary,
        "samples": samples,
    }


if __name__ == "__main__":
    raise SystemExit(main())
