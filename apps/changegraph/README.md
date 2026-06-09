# ai-convos-changegraph

Change graph over the convos DB ([spec 02](../../docs/specs/02-change-graph.md)):
*which conversation -- which prompt -- changed this file, or this line?*

Installs via the `convos.commands` entry-point group and registers under the `convos` CLI:

```bash
convos blame <file> [--line N] [--at <conv|ts>]   # per-line attribution
convos timeline <file>                            # chronological cross-provider edit history
convos at <file> <conv|ts>                        # reconstruct file content at that point
convos graph [<file>|<conv>] [-f json|dot]        # file <-> conversation edge set
```

Exactness boundary: attribution is computed by replaying captured `old_content` edits;
shell edits or missing/unmatched before-state make content *unknown* (never invented)
until the next full write. Reads the DB read-only; no core schema changes.
