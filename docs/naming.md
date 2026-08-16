---
summary: "Canonical product, package, repository, command, and skill names."
read_when:
  - Naming or publishing any Convos artifact
  - Writing installation instructions or product copy
  - Migrating legacy ai-convos names
status: accepted 2026-08-16; implementation pending
---

# Naming contract

The product is **Convos**, described as **queryable memory for coding agents**.
Because the name is generic, public metadata should pair it with that descriptor
where context is otherwise weak.

| Surface | Canonical name |
|---|---|
| Product | `Convos` |
| GitHub repository | `RobertBiehl/convos` |
| Primary PyPI distribution | `convos` |
| CLI command | `convos` |
| Bundled agent skill | `convos` |
| Default user data directory | `~/.convos` |
| Environment variable prefix | `CONVOS_` |
| Python import namespace | `ai_convos` |

The primary user installation must be named only `convos`; optional products
should be exposed as `convos[extra]` when possible, and any separately
installable companion distribution uses `convos-<product>`. Internal Python
imports may remain `ai_convos` and `ai_convos_<product>` because they are code
identifiers rather than product branding.

The skill's canonical name is `convos`; hosts may render its explicit invocation
as `/convos`, `$convos`, or `@convos`, but host syntax does not change the name.

Existing `ai-convos-db`, `ai-convos-*`, and `agent-convos` names are legacy
migration inputs, not aliases to preserve indefinitely. Until the rename lands,
current artifacts may still carry those names; new public copy must not introduce
additional uses of them.
