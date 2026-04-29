from __future__ import annotations

import json
from pathlib import Path

from cli_courier.chat_history import ChatHistory


def test_append_creates_jsonl_file(tmp_path: Path) -> None:
    h = ChatHistory(tmp_path / "chats" / "42.jsonl")
    h.append(role="user", text="hello")

    lines = h.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["role"] == "user"
    assert entry["text"] == "hello"
    assert "ts" in entry


def test_append_skips_blank_text(tmp_path: Path) -> None:
    h = ChatHistory(tmp_path / "42.jsonl")
    h.append(role="user", text="   ")
    assert not h.path.exists()


def test_tail_returns_last_n_entries(tmp_path: Path) -> None:
    h = ChatHistory(tmp_path / "42.jsonl")
    for i in range(10):
        h.append(role="user", text=f"msg {i}")

    tail = h.tail(3)
    assert len(tail) == 3
    assert tail[-1]["text"] == "msg 9"
    assert tail[0]["text"] == "msg 7"


def test_tail_on_missing_file_returns_empty(tmp_path: Path) -> None:
    h = ChatHistory(tmp_path / "missing.jsonl")
    assert h.tail(5) == []


def test_rotation_drops_oldest_lines(tmp_path: Path) -> None:
    h = ChatHistory(tmp_path / "42.jsonl", max_lines=5)
    for i in range(8):
        h.append(role="user", text=f"msg {i}")

    lines = h.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["text"] == "msg 3"
    assert json.loads(lines[-1])["text"] == "msg 7"


def test_tail_tolerates_corrupt_lines(tmp_path: Path) -> None:
    p = tmp_path / "42.jsonl"
    p.write_text('{"role":"user","text":"ok","ts":"x"}\nnot-json\n', encoding="utf-8")
    h = ChatHistory(p)
    tail = h.tail(10)
    assert len(tail) == 1
    assert tail[0]["text"] == "ok"
