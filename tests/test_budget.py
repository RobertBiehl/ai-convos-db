"""Tests for repository constraints."""

import token, tokenize
from pathlib import Path

TOKEN_WHITELIST = {token.OP, token.NAME, token.NUMBER, token.STRING}


def _loc(paths):
    return sum(len({t.start[0] for t in tokenize.generate_tokens(p.read_text().splitlines(True).__iter__().__next__)
                    if t.type in TOKEN_WHITELIST}) for p in paths)


def test_line_budget():
    """Keep total core LOC under 1000 (token-aware)."""
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "src" / "ai_convos").glob("*.py"))
    assert paths, "No source files found"
    loc = _loc(paths)
    assert loc < 1000, f"Code line budget exceeded: {loc} >= 1000"


def test_app_line_budgets():
    """Budget products honestly; never split one product into packages to evade its limit."""
    root = Path(__file__).resolve().parents[1]
    for src in sorted((root / "apps").glob("*/src")):
        loc = _loc(sorted(src.rglob("*.py")))
        limit = {"remote": 700, "remote_server": 275}.get(src.parent.name, 200)
        assert loc < limit, f"App {src.parent.name} budget exceeded: {loc} >= {limit}"


def test_remote_has_two_product_packages():
    root = Path(__file__).resolve().parents[1]
    assert {p.parent.name for p in (root / "apps").glob("remote*/pyproject.toml")} == {"remote", "remote_server"}
