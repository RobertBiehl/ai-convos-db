Skills Setup (Codex + Claude Code)
==================================

This repo ships one skill: `agent-convos`.

Install
-------

Run:

```bash
bin/install-skills
```

This installs to:
- Codex: `~/.codex/skills/agent-convos/SKILL.md`
- Claude Code: `~/.claude/skills/agent-convos/SKILL.md`

Optional: install only one
```bash
bin/install-skills codex
bin/install-skills claude
```

Use
---

Tell your agent to use the skill, e.g. "Use agent-convos to sync then search."

Just-in-time ingestion
----------------------

Install user-level lifecycle hooks for both coding agents:

```bash
convos install-hooks
convos install-hooks --status
```

Claude Code uses `Stop` plus `SessionEnd`; Codex uses `Stop`. Remove only these
ai-convos-db handlers with `convos install-hooks --remove`. Codex may require
reviewing the new command hook through `/hooks` after installation. Start a new
agent session, complete one turn, then use `convos doctor` to verify that
`ingest: ... last=...` is recent.
