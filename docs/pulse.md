---
summary: "Exact cross-project archive activity as a bounded digest or private local dashboard."
read_when:
  - Reviewing recent AI-assisted work across projects
  - Using or changing convos pulse
---

# Activity pulse

`convos pulse` answers one narrow question: where did recorded AI activity
happen recently? It groups archive conversations by their live Git root and
reports exact message, role, edit, distinct-file, and tool-status counts. It
does not read Git history, summarize message content, or infer whether work is
finished, successful, productive, or waiting for a reply.

By default, sessions with fewer than two current content-bearing messages are
omitted unless they captured an edit or tool call. This exact rule suppresses
automated probes without classifying message content. The output reports the
omitted count; `--min-messages 1` restores every recorded session.

Install the optional product:

```bash
uv tool install "ai-convos-db[pulse]"
convos pulse
```

The default is a Markdown digest for the last day. Each bounded recent session
includes its exact conversation ID, last recorded message ID and role, and a
`convos read` pivot. JSON exposes the same deterministic structure:

```bash
convos pulse -d 7 -n 20 --sessions 5
convos pulse -d 7 -f json
convos pulse -d 7 --min-messages 1
```

Create a self-contained dashboard with no server, scripts, fonts, or network
assets:

```bash
convos pulse -d 7 -f html -o ~/private/convos-pulse.html
convos pulse -f html --open
```

`--open` writes the default dashboard to `~/.convos/pulse/pulse.html`. Output
files are replaced atomically with mode `0600`; final-path symlinks are refused.
The HTML contains titles, paths, IDs, timestamps, and counts, so it is private
even though it contains no message excerpts. Delete it when its retention is no
longer useful.

## Boundaries

- Only non-empty, current messages inside the window count. Known injected
  agent scaffolding and superseded `history_of` rows do not.
- Existing cwd paths collapse to their current Git root. Existing non-Git
  directories and missing recorded paths remain separate exact scopes.
- Conversations without a cwd are omitted by default. `--include-web` adds
  them under `web`; it can combine unrelated browser conversations because the
  archive has no stronger project scope for them.
- File and tool counts reflect only events captured by source integrations.
  Distinct files are deduplicated per displayed project; edits are event counts.
- `--min-messages` is a metadata threshold, not an importance judgment.
  Sessions below it remain included when a captured edit or tool call exists.
- Tool statuses are reported exactly as stored, including `unknown`. Failure is
  not guessed from names, output, or later conversation text.
- Titles, paths, and other displayed metadata are control-character cleaned and
  passed through the local high-confidence secret scanner. This is not a
  guarantee against unknown, encoded, split, or ordinary-prose secrets.
- `-n` and `--sessions` bound presentation only. Top-level totals cover every
  matching project and conversation, and `projects_truncated` states what the
  display omitted.

Run `convos sync` if older web/export history must be refreshed. Installed
coding-agent hooks are drained automatically before each pulse.
