from __future__ import annotations

from pathlib import Path

import pytest

from cli_courier.filesystem import Sandbox, SandboxViolation


def make_sandbox(root: Path) -> Sandbox:
    return Sandbox(root, cat_max_bytes=32, sendfile_max_bytes=64)


def test_resolve_treats_absolute_paths_as_workspace_relative(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    sandbox = make_sandbox(tmp_path)

    assert sandbox.resolve("/notes.txt") == (tmp_path / "notes.txt").resolve()


def test_resolve_blocks_path_traversal(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path)

    with pytest.raises(SandboxViolation):
        sandbox.resolve("../")


def test_resolve_blocks_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-cli-courier.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(outside)
    sandbox = make_sandbox(tmp_path)

    with pytest.raises(SandboxViolation):
        sandbox.resolve("link")


def test_cat_blocks_sensitive_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    sandbox = make_sandbox(tmp_path)

    with pytest.raises(SandboxViolation, match="sensitive"):
        sandbox.cat_file(".env")


def test_cat_blocks_large_file(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("x" * 33, encoding="utf-8")
    sandbox = make_sandbox(tmp_path)

    with pytest.raises(SandboxViolation, match="too large"):
        sandbox.cat_file("large.txt")


def test_list_marks_sensitive_entries(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
    sandbox = make_sandbox(tmp_path)

    entries = sandbox.list_dir()
    assert entries[0].display_name == ".env.local [sensitive]"
