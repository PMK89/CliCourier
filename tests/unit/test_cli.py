from __future__ import annotations

from cli_courier.cli import normalize_remainder
from cli_courier.setup import infer_adapter


def test_normalize_remainder_strips_double_dash() -> None:
    assert normalize_remainder(["--", "codex", "--model", "x"]) == ["codex", "--model", "x"]


def test_infer_adapter_uses_codex_only_for_codex_command() -> None:
    assert infer_adapter("codex --model x") == "codex"
    assert infer_adapter("claude") == "generic"
    assert infer_adapter("gemini") == "generic"
