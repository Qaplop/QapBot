"""
Structural guard: every slash-command payload field must fit Discord's length limits.

Why this exists (2026-09-01 PROD incident): commit 1d85c7a lengthened one /admin
`action` choice name from 90 to 102 characters while fixing a doc inaccuracy
("+ CWL purge"). Discord rejects the *entire* command tree when any single field is
out of range:

    global_command_sync failed: 400 Bad Request (error code: 50035): Invalid Form Body
    In 9.options.0.choices.17.name: Must be between 1 and 100 in length.

`setup_hook()` treats that as fatal, so the bot did not start at all. Nothing caught
it for two commits because the failure only happens against the live Discord API, at
startup — there is no local validation in discord.py's builders, and no test touched
the payload shape.

This is a *structural* test on purpose (same reasoning as the `row[N]` guard described
in .github/copilot-instructions.md Cardinal Rule 14): a behavioural test cannot catch
it, because the code is correct right up until someone edits a string. Walking the real
command objects also means new commands are covered automatically, with nothing to keep
in sync.

Limits per Discord's application-command docs:
  - command / subcommand / option / choice NAME: 1-32 (choice names: 1-100)
  - command / option DESCRIPTION: 1-100
  - max 25 choices per option
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
from typing import Any, Iterator, List, Tuple

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

NAME_MAX = 32
CHOICE_NAME_MAX = 100
DESCRIPTION_MAX = 100
CHOICES_MAX = 25


def _iter_commands() -> Iterator[Tuple[str, Any]]:
    """Yield (dotted_path, command) for every app command reachable from QBdiscordcmds.

    Discovered by scanning the module namespace rather than mirroring QapBot.py's
    COMMANDS/COMMAND_GROUPS lists, so a newly added command is covered the moment it
    exists — a hand-maintained list here would be one more thing to forget.
    """
    from discord import app_commands
    import QBdiscordcmds

    def _walk(obj: Any, path: str) -> Iterator[Tuple[str, Any]]:
        if isinstance(obj, app_commands.Group):
            for sub in obj.commands:
                yield from _walk(sub, f"{path} {sub.name}")
        else:
            yield path, obj

    seen: set[int] = set()
    for attr in sorted(dir(QBdiscordcmds)):
        obj = getattr(QBdiscordcmds, attr, None)
        if not isinstance(obj, (app_commands.Command, app_commands.Group)):
            continue
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        yield from _walk(obj, f"/{obj.name}")


def test_command_discovery_finds_the_expected_commands() -> None:
    """Sanity check on the walker itself — if it silently found nothing, every
    assertion below would vacuously pass and the guard would be worthless."""
    paths = [p for p, _ in _iter_commands()]

    assert len(paths) >= 10, f"only discovered {len(paths)} commands: {paths}"
    assert "/admin" in paths


def test_command_names_and_descriptions_within_limits() -> None:
    violations: List[str] = []
    for path, cmd in _iter_commands():
        if not 1 <= len(cmd.name) <= NAME_MAX:
            violations.append(f"{path}: name len={len(cmd.name)} (limit {NAME_MAX})")
        description = getattr(cmd, "description", "") or ""
        if not 1 <= len(description) <= DESCRIPTION_MAX:
            violations.append(
                f"{path}: description len={len(description)} (limit {DESCRIPTION_MAX}): {description!r}"
            )

    assert not violations, "Discord rejects the whole command sync for any of these:\n  " + "\n  ".join(violations)


def test_option_names_and_descriptions_within_limits() -> None:
    violations: List[str] = []
    for path, cmd in _iter_commands():
        for param in getattr(cmd, "parameters", []):
            if not 1 <= len(param.name) <= NAME_MAX:
                violations.append(f"{path} option {param.name!r}: name len={len(param.name)} (limit {NAME_MAX})")
            description = param.description or ""
            if not 1 <= len(description) <= DESCRIPTION_MAX:
                violations.append(
                    f"{path} option {param.name!r}: description len={len(description)} "
                    f"(limit {DESCRIPTION_MAX}): {description!r}"
                )

    assert not violations, "Discord rejects the whole command sync for any of these:\n  " + "\n  ".join(violations)


def test_choice_names_within_limits() -> None:
    """The exact field that broke PROD startup on 2026-09-01."""
    violations: List[str] = []
    for path, cmd in _iter_commands():
        for param in getattr(cmd, "parameters", []):
            for index, choice in enumerate(param.choices or []):
                if not 1 <= len(choice.name) <= CHOICE_NAME_MAX:
                    violations.append(
                        f"{path} option {param.name!r} choice[{index}]: name len={len(choice.name)} "
                        f"(limit {CHOICE_NAME_MAX}): {choice.name!r}"
                    )

    assert not violations, "Discord rejects the whole command sync for any of these:\n  " + "\n  ".join(violations)


def test_choice_counts_within_limits() -> None:
    """Statically declared choices cap at 25 — past that an option needs autocomplete."""
    violations: List[str] = []
    for path, cmd in _iter_commands():
        for param in getattr(cmd, "parameters", []):
            count = len(param.choices or [])
            if count > CHOICES_MAX:
                violations.append(
                    f"{path} option {param.name!r}: {count} choices (limit {CHOICES_MAX} — use autocomplete)"
                )

    assert not violations, "Discord rejects the whole command sync for any of these:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize("length,valid", [(100, True), (101, False), (0, False)])
def test_limit_constants_match_discord_contract(length: int, valid: bool) -> None:
    """Pins the boundary the guard enforces, so a future edit that loosens
    CHOICE_NAME_MAX to make a failing test pass has to do so deliberately."""
    assert (1 <= length <= CHOICE_NAME_MAX) is valid
