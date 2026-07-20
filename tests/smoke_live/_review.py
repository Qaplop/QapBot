from __future__ import annotations

import asyncio
import time
from typing import Optional


async def maybe_wait_for_dev_review(*, channel, message, timeout_seconds: int) -> Optional[bool]:
    """Optionally wait for dev feedback via reactions.

Returns:
- True  -> ✅ received
- False -> ❌ received
- None  -> timed out / no review requested

Notes:
- Uses polling (REST fetch) so it doesn't rely on gateway reaction events/intents.
- Will proceed automatically if nobody reacts.
"""
    if timeout_seconds <= 0:
        return None

    try:
        await message.add_reaction("✅")
        await message.add_reaction("❌")
    except Exception:
        # If reactions cannot be added (permissions), fall back to a simple delay.
        await asyncio.sleep(min(timeout_seconds, 30))
        return None

    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            fetched = await channel.fetch_message(message.id)
        except Exception:
            return None

        for reaction in getattr(fetched, "reactions", []) or []:
            emoji = str(getattr(reaction, "emoji", ""))
            if emoji not in ("✅", "❌"):
                continue
            try:
                async for user in reaction.users(limit=25):
                    if getattr(user, "bot", False):
                        continue
                    return emoji == "✅"
            except Exception:
                # If we cannot enumerate users, keep polling until timeout.
                pass

        await asyncio.sleep(2)

    return None
