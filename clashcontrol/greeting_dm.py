"""Greeting DM for new users (tracker #0119).

One builder shared by `/dm me` (a user asks for it) and `/admin DM User` (a server admin sends it
to a member): a short welcome, what ClashControl does, the first commands, and the hint to /help.

The feature and "get started" texts are the Activity landing page's own (`activity.landing.*`),
so the DM and the /about page never describe the bot differently. Commands are rendered through
command_mention(), which makes them clickable in the DM. Like the landing page, the DM ends with
"Add to server" and "Open support channel" link buttons (tracker #0141).
"""

import logging
from typing import Literal, Optional, Tuple

import discord

from clashcontrol.cache_manager import CACHE
from clashcontrol.i18n import t  # type: ignore[attr-defined]

DmOutcome = Literal["sent", "blocked", "no_mutual_guild", "failed"]

# Landing-page feature bullets, in the page's own order.
_FEATURE_KEYS = (
    "feature_clan",
    "feature_cwl",
    "feature_predictions",
    "feature_notifications",
    "feature_stats",
)

# "Get started" commands for members, each paired with its landing-page description.
_GET_STARTED = (
    ("registration", "members_registration"),
    ("cwl preferences", "members_cwl"),
    ("help", "members_help"),
)


def build_greeting_dm_embed(user_id: str, guild_id: Optional[int], display_name: str) -> discord.Embed:
    """Build the greeting embed in the recipient's language.

    Args:
        user_id: Recipient's Discord user id — their own language preference wins.
        guild_id: Guild the greeting was requested from (language fallback), or None in a DM.
        display_name: Name to greet the recipient with.

    Returns:
        The embed, ready to send as a DM.
    """
    from clashcontrol.QBdiscocmdshelper import command_mention

    def tr(key: str, **kwargs: object) -> str:
        return t(key, guild_id=guild_id, user_id=user_id, **kwargs)

    embed = discord.Embed(
        title=tr('commands.dm.greeting_title', name=display_name)[:256],
        description=f"**{tr('activity.landing.tagline')}**\n{tr('activity.landing.intro')}",
        color=discord.Color.blue(),
    )
    features = "\n".join(f"• {tr(f'activity.landing.{key}')}" for key in _FEATURE_KEYS)
    features += f"\n🌐 {tr('activity.landing.feature_languages')}"
    embed.add_field(name=tr('commands.dm.features_title'), value=features[:1024], inline=False)

    get_started = "\n".join(
        f"{command_mention(cmd)} – {tr(f'activity.landing.{key}')}" for cmd, key in _GET_STARTED
    )
    embed.add_field(name=tr('activity.landing.members_title'), value=get_started[:1024], inline=False)
    # Tracker #0141: the invite itself is the "Add to server" button (build_greeting_dm_view()).
    embed.add_field(
        name=f"➕ {tr('activity.landing.install_title')}",
        value=tr('activity.landing.install_text')[:1024],
        inline=False,
    )
    embed.add_field(
        name="​",
        value=tr('commands.dm.help_hint', help=command_mention("help"), about=command_mention("about"))[:1024],
        inline=False,
    )
    embed.set_footer(text=tr('activity.landing.footer'))
    return embed


def build_greeting_dm_view(user_id: str, guild_id: Optional[int]) -> discord.ui.View:
    """The landing page's two link buttons for the greeting DM (tracker #0141): "Add to server"
    (bot_install_url) and "Open support channel". Link buttons never call back into the bot, so
    the view needs neither a timeout nor persistent registration.

    Args:
        user_id: Recipient's Discord user id (button labels follow their language).
        guild_id: Guild the greeting was requested from (language fallback), or None.
    """
    import QBcore
    from clashcontrol.constants import SUPPORT_INVITE_URL, bot_install_url

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=t('activity.landing.install_button', guild_id=guild_id, user_id=user_id)[:80],
        url=bot_install_url(QBcore.bot.application_id),
        emoji="➕",
    ))
    view.add_item(discord.ui.Button(
        label=t('activity.landing.support_button', guild_id=guild_id, user_id=user_id)[:80],
        url=SUPPORT_INVITE_URL,
        emoji="💬",
    ))
    return view


async def send_greeting_dm(
    user_id: str, guild_id: Optional[int], display_name: str
) -> Tuple[DmOutcome, Optional[discord.Message]]:
    """Send the greeting DM through CACHE.send_user_dm_detailed() (the logged DM choke point).

    Args:
        user_id: Recipient's Discord user id.
        guild_id: Guild the greeting was requested from, or None in a DM.
        display_name: Name to greet the recipient with.

    Returns:
        (outcome, sent message or None). outcome is send_user_dm_detailed()'s: "sent",
        "blocked" (recipient doesn't accept DMs from the bot), "no_mutual_guild" or "failed".
    """
    embed = build_greeting_dm_embed(user_id, guild_id, display_name)
    view = build_greeting_dm_view(user_id, guild_id)
    sent_out: list = []  # type: ignore[type-arg]
    _sent, outcome = await CACHE.send_user_dm_detailed(user_id, "", view=view, embed=embed, sent_message_out=sent_out)
    logging.info(f"[GREETING-DM] user={user_id} guild={guild_id} outcome={outcome}")
    return outcome, (sent_out[0] if sent_out else None)
