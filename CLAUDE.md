# QapBot — Instructions for Claude Code

This project's coding playbook lives in [.github/copilot-instructions.md](.github/copilot-instructions.md) — read it in full before making non-trivial changes. It covers cardinal rules (account-protection security checks, CACHE-only data access, i18n via `t()`, discord.py modal/interaction patterns, DB access rules), the changelog convention, the test-runner convention, and links to deep-dive docs under `qapbot/docs/` (architecture, DB schema, rate limiting, registration flows, war-file lifecycle).

## Standing routine: keep project knowledge in the repo, not private memory

When you learn a durable, general rule or convention during a session — something that would apply to any future session or any other tool/collaborator working on this repo, not just this one conversation — write it into `.github/copilot-instructions.md` or the relevant `qapbot/docs/*.md` file (create a new doc there if none fits), instead of only your private cross-session memory. Do this in the same turn you discover the rule, not as a follow-up.

Reserve private memory for what's genuinely personal or session-specific: your own rationale/history for *why* a rule exists, in-progress task context, or preferences that don't belong in a shared file. If you're unsure whether something is general-purpose or session-specific, default to writing it in the repo — that's the single source of truth every tool and model working on this project can see, since it's git-tracked and not tied to one machine or user.

## Standing rule: implementation plans live in the repo, never in a private plans dir

Never write an implementation plan file to a private/user-scoped location (e.g. `~/.claude/plans/`,
or any other plan-mode default path outside this repo). Always write it to `plans/` at this repo's
root instead, creating that directory if it doesn't exist yet.

Name the file so it's identifiable at a glance and, whenever the plan originates from a bug/feature
tracker item, references that item's number: `plans/tracker-NNNN-short-slug.md` (zero-padded to 4
digits, matching the tracker's `#NNNN` numbering). For plans not tied to a tracker item, use
`plans/short-slug.md`.

This applies regardless of which tool or planning flow produced the file (Claude Code's built-in
plan mode, an ad-hoc plan written on request, etc.) — the plan-mode tooling's default private path
is never the final destination for this project. If a plan mode or similar flow writes to a private
path first, copy/rewrite it into `plans/` in the repo as part of finishing that turn, not as a
follow-up someone has to remember to ask for.
