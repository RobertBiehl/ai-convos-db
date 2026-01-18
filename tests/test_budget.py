"""Tests for repository constraints."""

import token, tokenize
from pathlib import Path

TOKEN_WHITELIST = {token.OP, token.NAME, token.NUMBER, token.STRING}


def test_line_budget():
    """Keep total source LOC under 1000 (token-aware)."""
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "src" / "ai_convos").glob("*.py"))
    counts = [(p, [t for t in tokenize.generate_tokens(p.read_text().splitlines(True).__iter__().__next__)
                   if t.type in TOKEN_WHITELIST]) for p in paths]
    loc = sum(len({t.start[0] for t in toks}) for _, toks in counts)
    assert paths, "No source files found"
    assert loc < 1000, f"Code line budget exceeded: {loc} >= 1000"
