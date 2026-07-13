# Local insight recipes

These recipes run entirely against the local archive. They do not contact the
remote relay. Use conceptual discovery first, inspect bounded evidence, then use
SQL or graph views for structure and aggregation.

## Recover a decision or plan

```bash
convos query "What did we decide about encrypted team sharing and why?" -n 8 -c 200 -f jsonl
convos read CONVERSATION_ID --around MESSAGE_ID -n 24 -c 4000 -f jsonl
```

`query` handles paraphrased or uncertain wording. `read` verifies the relevant
neighborhood rather than treating a short retrieval snippet as the conclusion.

## Find an exact phrase, command, or filename

```bash
convos search "unobserved_change" -n 8 -c 200 -f jsonl
convos search "src/ai_convos/cli.py" -n 8 -c 200 -f jsonl
convos read CONVERSATION_ID --around MESSAGE_ID -n 20 -f jsonl
```

## Compare an idea across providers and time

```bash
convos query "How has our prompt-first source platform idea evolved?" -n 8 -c 200 -f jsonl
convos query "What requirements recur across our discussions of specs, prompts, and generated code?" -n 8 -c 200 -f jsonl
```

Read the strongest distinct conversations, then compare the dated evidence.
This can reconnect an old ChatGPT design discussion with later Claude Code or
Codex implementation work.

## Quantify the archive

```bash
convos sql "SELECT source, COUNT(*) conversations FROM conversations GROUP BY source ORDER BY conversations DESC" -f jsonl
convos sql "SELECT (SELECT COUNT(*) FROM messages) messages, (SELECT COUNT(*) FROM tool_calls) tool_calls, (SELECT COUNT(*) FROM file_edits) file_edits" -f jsonl
```

Use SQL for known fields, joins, dates, and counts. Text discovery is usually
better through `query` or `search`.

## Connect prompts to files and checkpoints

With the provenance application installed:

```bash
convos remote graph file_history --arg src/service.py
convos remote graph conversation_changes --arg CONVERSATION_ID
convos remote graph commit_conversations --arg GIT_COMMIT
convos remote graph checkpoint_diff --arg CHECKPOINT_A..CHECKPOINT_B
convos remote graph current_activity --arg /path/to/checkout
convos remote graph team_activity --arg WORKSPACE_ID\|/path/to/checkout
```

These views remain local and operate without server availability.
