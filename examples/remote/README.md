# Remote usage examples

These examples run the real relay, clients, background workers, encryption, and
local projections using synthetic data in temporary directories. They never
read the normal Convos archive and use network access only over loopback.

```bash
uv sync --all-extras
uv run python examples/remote/demo.py personal
uv run python examples/remote/demo.py team
uv run python examples/remote/demo.py all
```

The personal scenario enrolls two devices, writes a synthetic conversation on
one device, and waits for automatic delivery to the other without calling
`convos remote sync`.

The team scenario links a Git repository once, clones it under an unrelated
absolute path for another user, and proves that the relevant prompt, edit, and
graph activity arrive automatically. Both scenarios inspect the relay database
to confirm their prompt and checkout paths are absent from server storage.

Add `--keep` to preserve the generated directory for inspection. Without it,
all identities, keys, databases, repositories, and server state are removed at
the end of the run.

For deployment and real account commands, see [the remote operations guide](../../docs/remote.md).
