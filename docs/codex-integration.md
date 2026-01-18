---
summary: "Codex CLI integration: session files, JSONL format, and function calls."
read_when:
  - Syncing Codex sessions
  - Understanding Codex log format
  - Debugging Codex sync issues
---

# Codex Integration

Parses local Codex CLI session files from `~/.codex/sessions/`.

## Session Files

Codex stores sessions organized by date:

```
~/.codex/
└── sessions/
    └── 2024/
        └── 01/
            └── 16/
                ├── rollout-2024-01-16T00-01-52-uuid.jsonl
                └── rollout-2024-01-16T12-30-00-uuid.jsonl
```

## JSONL Format

Each line is a JSON object with `type` field.

### Session Metadata

First line contains session info:

```json
{
  "timestamp": "2024-01-16T00:01:52.679Z",
  "type": "session_meta",
  "payload": {
    "id": "uuid",
    "timestamp": "2024-01-16T00:01:52.655Z",
    "cwd": "/Users/name/project",
    "originator": "codex_cli_rs",
    "cli_version": "0.85.0",
    "model_provider": "openai"
  }
}
```

### Response Items

Messages and function calls:

**User message:**
```json
{
  "timestamp": "2024-01-16T00:01:53Z",
  "type": "response_item",
  "payload": {
    "type": "message",
    "role": "user",
    "content": [
      {"type": "input_text", "text": "Fix the bug"}
    ]
  }
}
```

**Assistant message:**
```json
{
  "type": "response_item",
  "payload": {
    "type": "message",
    "role": "assistant",
    "content": [
      {"type": "output_text", "text": "I'll fix that."}
    ]
  }
}
```

**Function call:**
```json
{
  "type": "response_item",
  "payload": {
    "type": "function_call",
    "name": "shell",
    "arguments": {
      "command": "cat file.py"
    }
  }
}
```

**Function output:**
```json
{
  "type": "response_item",
  "payload": {
    "type": "function_call_output",
    "call_id": "call_xyz",
    "output": "file contents..."
  }
}
```

## Extracted Data

### Conversations

- `id`: Hash of file path
- `source`: `codex`
- `title`: Working directory or session stem
- `cwd`: From session_meta payload
- `model`: From model_provider (usually "openai")

### Messages

- User/assistant messages from `response_item` with `type: message`
- Developer and system messages are filtered out
- Content extracted from `input_text`, `output_text`, or `text` blocks

### Tool Calls

Function calls mapped to tool_calls:
- `tool_name`: Function name (e.g., `shell`)
- `input`: Function arguments
- `output`: From function_call_output

### File Edits

Extracted from shell commands that modify files:
- Pattern matching for `cat >`, `echo >`, `sed`, `awk`
- Edit type: `shell`
- Content: Full command

## Usage

```bash
# Sync all Codex sessions
uv run convos sync

# Import specific Codex directory
CONVOS_IMPORT_PATHS="~/.codex" uv run convos sync

# Search Codex sessions only
uv run convos search "python" -s codex

# List by source
uv run convos list -s codex
```

## Differences from Claude Code

| Aspect | Claude Code | Codex |
|--------|-------------|-------|
| Provider | Anthropic | OpenAI |
| Event types | system, human, assistant | session_meta, response_item |
| Tool format | tool_use/tool_result blocks | function_call/function_call_output |
| Thinking | Explicit thinking blocks | Not available |
| File edits | Write/Edit tools | Shell command patterns |

## Troubleshooting

**No sessions found:**
- Check `~/.codex/sessions/` exists
- Codex may store sessions elsewhere on some systems

**Missing function outputs:**
- Some function calls may not have recorded output
- Status shows as "pending" for incomplete calls

**Duplicate sessions:**
- IDs based on file path ensure no duplicates
- Re-syncing updates existing records
