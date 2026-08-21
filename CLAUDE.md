# QapBot — Instructions for Claude Code

This project's coding playbook lives in [.github/copilot-instructions.md](.github/copilot-instructions.md) — read it in full before making non-trivial changes. It covers cardinal rules (account-protection security checks, CACHE-only data access, i18n via `t()`, discord.py modal/interaction patterns, DB access rules), the changelog convention, the test-runner convention, and links to deep-dive docs under `qapbot/docs/` (architecture, DB schema, rate limiting, registration flows, war-file lifecycle).

## Standing routine: keep project knowledge in the repo, not private memory

When you learn a durable, general rule or convention during a session — something that would apply to any future session or any other tool/collaborator working on this repo, not just this one conversation — write it into `.github/copilot-instructions.md` or the relevant `qapbot/docs/*.md` file (create a new doc there if none fits), instead of only your private cross-session memory. Do this in the same turn you discover the rule, not as a follow-up.

Reserve private memory for what's genuinely personal or session-specific: your own rationale/history for *why* a rule exists, in-progress task context, or preferences that don't belong in a shared file. If you're unsure whether something is general-purpose or session-specific, default to writing it in the repo — that's the single source of truth every tool and model working on this project can see, since it's git-tracked and not tied to one machine or user.

Concretely, that means the actual rule text belongs in [.github/copilot-instructions.md](.github/copilot-instructions.md) or `qapbot/docs/*.md` — not written out here in CLAUDE.md itself. CLAUDE.md is Claude Code's own entry point (only Claude Code reads it directly), while copilot-instructions.md is the tool-agnostic playbook any collaborator or tool works from; a rule that should bind "any future session or any other tool" isn't actually general-purpose yet if it only lives here. See copilot-instructions.md's Cardinal Rule 15 for the implementation-plan storage convention and the "manual test case per tracker item" workflow step.
