Contributing
============

Thanks for helping! This project is small and intentionally simple. Please keep PRs focused.

Setup
-----

```bash
uv run convos init
```

Make a change
------------

- Keep `src/ai_convos/cli.py` readable and mostly functional.
- Avoid adding dependencies unless they provide clear value.
- Prefer explicit, normalized fields in parsers and fetchers.

Manual validation
-----------------

```bash
uv run convos init
uv run convos import /path/to/export
uv run convos search "test"
uv run convos stats
```

Style
-----

- ASCII-only unless a file already uses Unicode.
- Comments only for non-obvious logic.
- Favor straightforward data transforms.

Submitting
----------

- Open a PR with a short description and a small test plan.
