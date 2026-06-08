---
summary: "Spec: the conversation change tracker -- which conversation/prompt changed each file or line. A minimal core capture plus a change-graph application."
read_when:
  - Implementing file blame / timeline / time-travel over conversations
  - Capturing edit before-state (old_content) at ingest
  - Understanding the exactness boundary for line-level attribution
status: draft (2026-06-06)
---

# Change graph (spec 02)

Part of [00-overview](00-overview.md). The flagship feature: *"which conversation
-- which prompt -- changed this file, or this line?"* Split into a minimal
**core capture** and a **change-graph application**, per the boundary rule.

## What already exists

`file_edits` links `message -> file_path -> edit_type -> content -> created_at`
(cli.py ~:68), and `conversations` carry `cwd` + `git_branch` (claude-code ~:505,
codex ~:556). The join is already used by `export` (~:808) and `stats` (~:950).
So the graph's nodes (file, edit, message, conversation) and most edges already
exist in the DB -- this feature is largely *queries over data you already store*.

## The two gaps

1. **Only the NEW text is stored.** `file_edits.content` is the new content /
   `new_string` (cli.py ~:497); `old_string` is discarded. Without the "before",
   diffs and line ranges cannot be computed reliably.
2. **Shell edits are lossy.** Codex shell edits are regex-scraped to a *guessed*
   path (cli.py ~:546) and the stored content is the command, not the change. So
   shell-driven edits are often invisible or unattributable.

## CORE change (capture only -- the one thing core owns here)

Add `old_content TEXT` (nullable) to `file_edits`, populated from tool input we
already parse:

- **Edit / MultiEdit:** `old_string` (claude-code `make_edits`, cli.py ~:493).
- **Write:** `NULL` (no prior in the call) -- or, optionally, the previous
  `file_edits.content` for that path.
- **shell:** `NULL` (unknown).

This is the only core change: it is ingest-time and **unreconstructable later**,
so it qualifies under the boundary rule. ~3-4 LoC + an `ALTER` migration + a
parser test. Everything below is the application.

## APPLICATION: `ai-convos-changegraph` (separate package, ~100 LoC)

Installs via the plugin seam (see [00](00-overview.md)); reads the DB only.
Commands (registered under the `convos` CLI):

- `convos blame <file> [--line N] [--at <conv|ts>]` -- per-line attribution:
  which conversation, which prompt (message), which provider, when.
- `convos timeline <file>` -- chronological edits across conversations and
  providers (the cross-provider file history; the roadmap's "file timeline").
- `convos at <file> <conv|ts>` -- reconstruct the file's content as of that
  point (time-travel).
- `convos graph [<file>|<conv>] --json|--dot` -- emit the
  file <-> conversation <-> prompt edge set for external graphing.

## Exactness boundary (respects "no approximations")

- **Write/Edit with `old_content` captured:** line attribution is **exact** --
  replay the captured edits against the text.
- **Git-anchored:** when `cwd` is a git repo, correlate edit timestamps + content
  with commits / `git blame` for **exact** commit <-> conversation links.
- **Shell or missing `old_content`:** present as *"edit occurred at T by
  conversation C, content unknown"* -- never invent a line number or a diff. No
  fabricated numbers; unknown spans are labeled unknown.

## Reconstruction algorithm (sketch)

Order a file's edits by `created_at`. `Write` sets full content; `Edit` applies
`old_content -> content`, anchored by matching `old_content` in the current
text. Replay up to a target conversation/timestamp = **time-travel** (`at`).
Per-line provenance = the last edit whose new text produced that line. Where
`cwd` is a git repo, prefer git-anchoring for exactness; otherwise label spans
with no captured before-state as unknown. Recompute on demand (cheap at current
scale); do not persist snapshots.

## Open questions

- **Codex shell fidelity:** parse `apply_patch` / heredocs to recover real diffs
  instead of regex-guessing a path? (Raises shell attribution; deserves its own
  sub-spec.)
- **Git correlation key:** match by content hash, by timestamp window, or by a
  commit-message trailer convention written at commit time?
- **Cross-tool sessions:** link claude-code/codex sessions sharing a `cwd` +
  branch that continue each other, so a file's history spans tools cleanly.
