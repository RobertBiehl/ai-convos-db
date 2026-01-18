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
