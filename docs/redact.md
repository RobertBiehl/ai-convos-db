# Local secret protection

`convos-redact` provides two related guarantees:

- `convos redact scan` finds high-confidence secret shapes in the local archive
  without printing or storing their values.
- `convos-remote` requires the package and applies its policy to every team
  record inside `publish`, before the event is signed or encrypted.

Personal workspace synchronization remains lossless. The relay never performs
scanning because it never receives plaintext.

## Install and inspect

Install the scanner on its own:

```bash
uv tool install "convos[redact]"
convos redact scan
convos redact scan -f json
```

Installing `convos-remote` brings the same package as a required dependency;
team protection is not an optional runtime switch. Inspect what automatic team
projection has removed:

```bash
convos redact status
convos redact status -f json
```

Both commands report only a secret kind, record identity, JSON field path, and
line number. They never return the matching bytes or an excerpt. Automatic
audit state is a mode-0600 SQLite database under
`$CONVOS_PROJECT_ROOT/redact/audit.db`.

The first full scan reads every potentially sensitive text field. Its result is
cached in a private value-free JSON file keyed by the exact archive path,
nanosecond modification time, and byte size. Repeating the command against the
unchanged database is immediate; any normal archive write invalidates the
cache. Use `--fresh` to force a new pass. On the development archive (about 827
MB of searchable text), the uncached pass took about 46 seconds; archive shape
and hardware will vary.

## What is protected

The scanner recognizes private-key blocks and high-confidence forms for
Anthropic, OpenAI, GitHub, GitLab, AWS, Google, Slack, Stripe, PyPI, npm, JWT,
credential-bearing URLs, and explicit password/token/key assignments. It walks
nested record payloads, including message content and thinking, tool input and
output, metadata, artifacts, and old and new file-edit bodies.

Every matching span becomes a typed marker such as
`[REDACTED:github_token]`. The marker contains no hash of the secret, so it
cannot be used for offline guessing or cross-record secret correlation.
Redaction happens before payload revision hashing, event signing, and
encryption. Repeated scans therefore converge on the same projected record.

Team binary bodies are omitted because a binary format can hide or split
credentials in ways a dependency-free text scanner cannot prove safe, including
across chunks. The attachment row remains as an explicit
`[REDACTED:attachment]` placeholder with sensitive metadata cleared, so a shared
conversation never acquires an invisible structural hole. Personal workspaces
retain attachments unchanged. Team projection does not read attachment bodies.

## Exact boundary and limitations

This is a high-confidence guardrail, not a general data-loss-prevention claim.
Unknown token formats, ordinary prose secrets, encoded or encrypted values, and
credentials assembled across records may not match. Run the local scan before
linking a sensitive repository and review the actual team projection policy.

False positives are redacted rather than allowed through. There is deliberately
no command that allowlists a matching value: such a bypass would turn a stale
local exception into a future secret leak. If a non-secret identifier resembles
a supported credential, the shared archive contains its typed marker while the
personal archive remains complete.

The policy protects events published after installation. It does not rewrite
team plaintext already received by another member, old relay ciphertext,
client or relay backups, or old epoch keys already granted. Removing a member
and rotating keys prevents future access but cannot erase material that member
already obtained. Treat any previously shared secret as exposed and rotate it.
