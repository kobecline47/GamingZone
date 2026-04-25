import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import timedelta
import random
import asyncio
import collections
import os
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass
import re
import urllib.request
import urllib.parse
import json
import traceback
import time
import ctypes.util
import tempfile
import nacl.secret  # required for discord voice (PyNaCl)
import davey        # required for discord voice (DAVE E2EE protocol)
import dashboard
import pokemon_game
import gambling

# Ensure FFmpeg is on PATH (Windows only — on Linux/Railway it is installed system-wide)
import sys
if sys.platform == "win32":
    _ffmpeg_dir = r"C:\Users\kobec\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
    if _ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

GUILD_ID  = discord.Object(id=711335159189864468)
GUILD_ID_2 = discord.Object(id=1495449662755442698)


class Client(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._feature_cmds_registered = False

    async def on_member_join(self, member: discord.Member):
        # ── Invite tracking ───────────────────────────────────────────────
        try:
            new_invites = await member.guild.invites()
            old_cache = INVITE_CACHE.get(member.guild.id, {})
            inviter = None
            for inv in new_invites:
                old = old_cache.get(inv.code)
                if old and inv.uses > old.uses:
                    inviter = inv.inviter
                    INVITE_COUNTS.setdefault(member.guild.id, {})
                    new_count = INVITE_COUNTS[member.guild.id].get(inviter.id, 0) + 1
                    INVITE_COUNTS[member.guild.id][inviter.id] = new_count
                    break
            INVITE_CACHE[member.guild.id] = {inv.code: inv for inv in new_invites}
        except Exception:
            inviter = None

        # ── Greeting card in #welcome ─────────────────────────────────────
        welcome_ch = discord.utils.get(member.guild.text_channels, name="welcome")
        if not welcome_ch:
            welcome_ch = discord.utils.get(member.guild.text_channels, name="general")
        if welcome_ch:
            titles = [
                "A new player has entered the server!",
                "A new recruit has arrived!",
                "Someone just joined the party!",
                "A legend has appeared!",
            ]
            embed = discord.Embed(
                title=random.choice(titles),
                description=(
                    f"Welcome, {member.mention}! 👋\n\n"
                    f"You are member **#{member.guild.member_count}**.\n"
                    f"Check out the rules and enjoy your stay!"
                ),
                color=0x2ECC71,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            if inviter:
                embed.set_footer(text=f"Invited by {inviter} · Joined {member.guild.name}")
            else:
                embed.set_footer(text=f"Joined {member.guild.name}")
            embed.timestamp = discord.utils.utcnow()
            await welcome_ch.send(embed=embed)

        # ── Social alert in #social-alerts ───────────────────────────────
        alert_ch = discord.utils.get(member.guild.text_channels, name="social-alerts")
        if alert_ch:
            desc = f"📥 {member.mention} **joined the server** — member #{member.guild.member_count}"
            if inviter:
                desc += f"\n🔗 Invited by **{inviter}**"
            embed = discord.Embed(description=desc, color=0x2ECC71)
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            await alert_ch.send(embed=embed)

    async def on_member_remove(self, member: discord.Member):
        # ── Social alert when someone leaves ─────────────────────────────
        alert_ch = discord.utils.get(member.guild.text_channels, name="social-alerts")
        if alert_ch:
            embed = discord.Embed(
                description=f"📤 **{member}** left the server.",
                color=0xE74C3C,
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            await alert_ch.send(embed=embed)

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # ── Social alert when someone boosts ─────────────────────────────
        if before.premium_since is None and after.premium_since is not None:
            alert_ch = discord.utils.get(after.guild.text_channels, name="social-alerts")
            if alert_ch:
                embed = discord.Embed(
                    title="💎 New Server Boost!",
                    description=f"{after.mention} just boosted the server! Thank you! 🎉",
                    color=0xFF73FA,
                )
                embed.set_thumbnail(url=after.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                await alert_ch.send(embed=embed)
        # ── Log role changes ──────────────────────────────────────────────
        if before.roles != after.roles:
            added   = [r for r in after.roles  if r not in before.roles]
            removed = [r for r in before.roles  if r not in after.roles]
            if added or removed:
                log_ch = after.guild.get_channel(LOG_CHANNEL_ID)
                if log_ch:
                    embed = discord.Embed(title="🏷️ Role Update", color=0x3498DB)
                    embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                    if added:
                        embed.add_field(name="Added", value=" ".join(r.mention for r in added), inline=False)
                    if removed:
                        embed.add_field(name="Removed", value=" ".join(r.mention for r in removed), inline=False)
                    embed.timestamp = discord.utils.utcnow()
                    await log_ch.send(embed=embed)

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        gid, uid = member.guild.id, member.id

        # ── Personal Space logic ──────────────────────────────────────────────
        lobby_id = PERSONAL_SPACE_LOBBY.get(gid)

        # User joined the lobby trigger channel → create their private VC
        if lobby_id and after.channel and after.channel.id == lobby_id:
            guild = member.guild
            category = after.channel.category
            # Create a private channel: only the owner + admins can see it by default
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
                member: discord.PermissionOverwrite(connect=True, view_channel=True, manage_channels=True, mute_members=True, deafen_members=True, move_members=True),
            }
            # Admins keep their perms
            for role in guild.roles:
                if role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(connect=True, view_channel=True)
            try:
                new_ch = await guild.create_voice_channel(
                    name=f"🔒 {member.display_name}'s Space",
                    category=category,
                    overwrites=overwrites,
                    reason="Personal Space auto-created",
                )
                PERSONAL_SPACE_CHANNELS[new_ch.id] = uid
                await member.move_to(new_ch)
            except Exception as e:
                print(f"[PersonalSpace] Could not create channel: {e}")

        # User left a personal space channel → delete if empty
        if before.channel and before.channel.id in PERSONAL_SPACE_CHANNELS:
            ch = before.channel
            # Give Discord a moment to update member list
            await asyncio.sleep(1)
            try:
                ch = member.guild.get_channel(ch.id)
                if ch and len(ch.members) == 0:
                    PERSONAL_SPACE_CHANNELS.pop(ch.id, None)
                    await ch.delete(reason="Personal Space empty — auto-deleted")
            except Exception as e:
                print(f"[PersonalSpace] Could not delete channel: {e}")

        # ── XP voice tracking ─────────────────────────────────────────────────
        # Joined a voice channel
        if before.channel is None and after.channel is not None:
            VOICE_JOIN_TIME.setdefault(gid, {})[uid] = time.time()
        # Left a voice channel
        elif before.channel is not None and after.channel is None:
            join_t = VOICE_JOIN_TIME.get(gid, {}).pop(uid, None)
            if join_t:
                minutes = int((time.time() - join_t) / 60)
                vm = VOICE_MINUTES.setdefault(gid, {})
                vm[uid] = vm.get(uid, 0) + minutes

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild:
            return
        if before.content == after.content:
            return
        log_ch = before.guild.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="✏️ Message Edited", color=0xF1C40F)
            embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
            embed.add_field(name="Channel", value=before.channel.mention, inline=False)
            embed.add_field(name="Before", value=before.content[:1024] or "*empty*", inline=False)
            embed.add_field(name="After",  value=after.content[:1024]  or "*empty*", inline=False)
            embed.add_field(name="Jump",   value=f"[Go to message]({after.jump_url})", inline=False)
            embed.timestamp = discord.utils.utcnow()
            await log_ch.send(embed=embed)

    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="🗑️ Message Deleted", color=0xE74C3C)
            embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
            embed.add_field(name="Channel", value=message.channel.mention, inline=False)
            embed.add_field(name="Content", value=message.content[:1024] or "*empty*", inline=False)
            embed.timestamp = discord.utils.utcnow()
            await log_ch.send(embed=embed)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.message_id not in REACTION_ROLES:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        emoji_str = str(payload.emoji)
        role_id = REACTION_ROLES[payload.message_id].get(emoji_str)
        if not role_id:
            return
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        if role and member and not member.bot:
            await member.add_roles(role, reason="Reaction role")
            await log_role_change(guild, member, role, added=True, source=f"Reaction {emoji_str}")

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.message_id not in REACTION_ROLES:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        emoji_str = str(payload.emoji)
        role_id = REACTION_ROLES[payload.message_id].get(emoji_str)
        if not role_id:
            return
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        if role and member and not member.bot:
            await member.remove_roles(role, reason="Reaction role removed")
            await log_role_change(guild, member, role, added=False, source=f"Reaction {emoji_str}")

    async def on_ready(self):
        print(f'Logged on as {self.user}!')
        if not discord.opus.is_loaded():
            try:
                opus_lib = None
                if _platform.system() == "Windows":
                    discord.opus._load_default()
                else:
                    for candidate in (ctypes.util.find_library("opus"), "libopus.so.0", "libopus.so"):
                        if not candidate:
                            continue
                        try:
                            discord.opus.load_opus(candidate)
                            opus_lib = candidate
                            break
                        except Exception:
                            continue
                    if not discord.opus.is_loaded():
                        discord.opus._load_default()
                print('Opus loaded successfully.')
                if opus_lib:
                    print(f'Loaded Opus library: {opus_lib}')
            except Exception as e:
                print(f'Failed to load Opus: {e}')
        try:
            # Register persistent views so buttons survive restarts
            client.add_view(GamerVerifyView())
            client.add_view(GameRoleView())
            client.add_view(TicketView())
            client.add_view(OpenTicketView())
            # Cache invites for all guilds
            for guild in self.guilds:
                try:
                    invites = await guild.invites()
                    INVITE_CACHE[guild.id] = {inv.code: inv for inv in invites}
                except Exception:
                    pass
            # Start background tasks once
            if not giveaway_check.is_running():
                giveaway_check.start()
            if not streamer_check.is_running():
                streamer_check.start()
            if not free_games_check.is_running():
                free_games_check.start()
            if not empty_vc_cleanup.is_running():
                empty_vc_cleanup.start()

            # Register grouped feature commands once (global)
            if not self._feature_cmds_registered:
                pokemon_game.setup_pokemon(self)
                pokemon_game.setup_pokemon_economy(self)
                gambling.setup_gambling(self)
                self._feature_cmds_registered = True

            # Ensure private log channels exist in all guilds
            for g in self.guilds:
                try:
                    await _ensure_log_channels(g)
                    await _ensure_feature_channels(g)
                    await _post_verify_embed(g)
                except Exception as e:
                    print(f"[Logs] Could not create channels in {g.name}: {e}")

            # Global sync plus per-guild fast sync so new servers get commands immediately
            synced_global = await self.tree.sync()
            print(f'Synced {len(synced_global)} global slash commands.')
            for g in self.guilds:
                try:
                    synced_guild = await self.tree.sync(guild=g)
                    print(f'Synced {len(synced_guild)} slash commands to guild {g.id}')
                except Exception as e:
                    print(f"[Sync] Could not sync guild {g.id}: {e}")
            # Inject the running event loop into the dashboard now that the bot is connected
            dashboard._state["bot_loop"] = asyncio.get_event_loop()
        except Exception as e:
            print(f'Error syncing commands: {e}')

    async def on_guild_join(self, guild: discord.Guild):
        try:
            await _ensure_log_channels(guild)
            await _ensure_feature_channels(guild)
            await _post_verify_embed(guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[Guild Join] Synced {len(synced)} slash commands to {guild.name} ({guild.id})")
        except Exception as e:
            print(f"[Guild Join] Setup/sync failed for {guild.name} ({guild.id}): {e}")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True
intents.presences = True
intents.moderation = True

client = Client(command_prefix="!", intents=intents)

LOG_CHANNEL_ID = 1495573126304497735
MOD_LOG_NAME  = "mod-logs"   # admin-only mod action log
ROLE_LOG_NAME = "role-logs"  # admin-only role assignment log
GAMBLING_CHANNEL_NAME = "casino-floor"   # dedicated casino text channel
POKEMON_CHANNEL_NAME  = "pokemon-battle" # dedicated pokemon battle channel
GAMER_ROLE_NAME       = "Gamer"          # role granted on verification — unlocks feature channels
VERIFY_CHANNEL_NAME   = "✅-verify"       # visible to unverified; hidden once Gamer role is granted
MUSIC_CATEGORY_NAME   = "♦┃𝙏𝙚𝙭𝙩 𝘾𝙝𝙖𝙣𝙣𝙚𝙡𝙨┃♦"  # category where music-channel is created
WHITELIST: set[int] = set()  # stores whitelisted user IDs


async def _ensure_log_channels(guild: discord.Guild) -> None:
    """Create mod-logs and role-logs with admin-only visibility if they don't exist."""
    admin_ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True
        ),
    }
    for role in guild.roles:
        if role.permissions.administrator:
            admin_ow[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True
            )
    for ch_name, topic in [
        (MOD_LOG_NAME,  "🔨 Private admin log — every mod command is recorded here."),
        (ROLE_LOG_NAME, "🏷️ Private role log — all role picks from embeds are recorded here."),
    ]:
        if not discord.utils.get(guild.text_channels, name=ch_name):
            await guild.create_text_channel(ch_name, overwrites=admin_ow, topic=topic)
            print(f"[Logs] Created #{ch_name} in {guild.name}")

async def _ensure_feature_channels(guild: discord.Guild) -> None:
    """Auto-create casino, pokemon-battle, and music-channel locked to the Gamer role."""
    # Get or create the Gamer role
    gamer_role = discord.utils.get(guild.roles, name=GAMER_ROLE_NAME)
    if not gamer_role:
        gamer_role = await guild.create_role(
            name=GAMER_ROLE_NAME,
            colour=discord.Colour.green(),
            reason="Auto-created: grants access to feature channels after verification",
        )
        print(f"[Verify] Created @{GAMER_ROLE_NAME} role in {guild.name}")

    # @everyone cannot see these channels; only Gamer role members can
    restricted_ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        gamer_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True
        ),
    }
    # Find the target category for the music channel (case-insensitive match)
    music_category = discord.utils.find(
        lambda c: c.name.lower() == MUSIC_CATEGORY_NAME.lower(), guild.categories
    )

    channels = [
        (GAMBLING_CHANNEL_NAME, "🎰 Use all casino commands here! /slots /blackjack /poker /crash and more", None),
        (POKEMON_CHANNEL_NAME,  "⚔️ Challenge others to Pokemon battles here! /pokemon battle", None),
        (MUSIC_CHANNEL_NAME,    "🎵 Request music and use all music commands here! /play /skip /queue", music_category if music_category else None),
    ]
    for ch_name, topic, category in channels:
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if ch:
            # Update existing channel perms (and move to correct category if set)
            edit_kwargs = {"overwrites": restricted_ow}
            if category and ch.category != category:
                edit_kwargs["category"] = category
            await ch.edit(**edit_kwargs)
        else:
            ch = await guild.create_text_channel(ch_name, overwrites=restricted_ow, topic=topic, category=category)
            print(f"[Channels] Created #{ch_name} in {guild.name}" + (f" under '{category.name}'" if category else ""))

    # Verify channel: visible to @everyone, hidden once they have the Gamer role
    # Admins always keep visibility so they can manage it
    verify_ow = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True
        ),
        gamer_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True
        ),
    }
    for role in guild.roles:
        if role.permissions.administrator:
            verify_ow[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
    verify_ch = discord.utils.get(guild.text_channels, name=VERIFY_CHANNEL_NAME)
    if verify_ch:
        await verify_ch.edit(overwrites=verify_ow)
    else:
        verify_ch = await guild.create_text_channel(
            VERIFY_CHANNEL_NAME,
            overwrites=verify_ow,
            topic="🟢 Click the button to verify and unlock the server!",
        )
        print(f"[Channels] Created #{VERIFY_CHANNEL_NAME} in {guild.name}")

async def _post_verify_embed(guild: discord.Guild) -> None:
    """Post the verification embed in #verify (visible to new members, hidden after they verify)."""
    verify_ch = discord.utils.get(guild.text_channels, name=VERIFY_CHANNEL_NAME)
    if not verify_ch:
        return

    # Don't repost if there's already a bot message with the verify embed
    try:
        async for msg in verify_ch.history(limit=20):
            if msg.author == guild.me and msg.embeds and msg.embeds[0].title and "✅" in msg.embeds[0].title:
                return
    except Exception:
        pass

    embed = discord.Embed(
        title="✅  Welcome — Verify to Get Access!",
        description=(
            f"Click the button below to receive the **@{GAMER_ROLE_NAME}** role.\n\n"
            "Once verified you'll unlock:\n"
            f"🎰 **#{GAMBLING_CHANNEL_NAME}** — Casino games\n"
            f"⚔️ **#{POKEMON_CHANNEL_NAME}** — Pokemon battles\n"
            f"🎵 **#{MUSIC_CHANNEL_NAME}** — Music commands\n\n"
            "*This channel will disappear once you verify — out of sight, out of mind!*"
        ),
        color=0x2ECC71,
    )
    embed.set_footer(text="GamingZoneBot • Click once to verify")
    try:
        await verify_ch.send(embed=embed, view=GamerVerifyView())
        print(f"[Verify] Posted verification embed in #{VERIFY_CHANNEL_NAME} in {guild.name}")
    except Exception as e:
        print(f"[Verify] Could not post embed in {guild.name}: {e}")

# ── Auto-Moderation ───────────────────────────────────────────────────────────
BANNED_WORDS: set[str] = {
    # Racial slurs
    "nigger", "nigga", "nig", "negro", "spic", "spick", "wetback",
    "chink", "gook", "slant", "zipperhead", "towelhead", "raghead",
    "sandnigger", "beaner", "cracker", "honkey", "honky", "kike",
    "hymie", "jap", "nip", "coon", "pickaninny", "sambo", "porch monkey",
    "redskin", "injun", "squaw", "halfbreed",
    # Homophobic / transphobic slurs
    "faggot", "fag", "dyke", "tranny", "shemale",
    # Ableist slurs
    "retard", "retarded",
    # Other hate / threats
    "kys", "kill yourself", "go kill yourself",
}
spam_tracker: dict[int, list] = {}  # user_id -> list of message timestamps
SPAM_THRESHOLD = 5    # messages
SPAM_WINDOW    = 5    # seconds
BANNED_WORD_WARNINGS: dict[int, int] = {}  # user_id -> warning count (max 2 before ban)
AD_WARNINGS: dict[int, int] = {}           # user_id -> ad warning count (1=timeout, 2=kick)

import datetime

GAMERTAGS: dict[int, dict[str, str]] = {}        # user_id -> {platform -> tag}
LFG_CHANNEL_NAME = "looking-for-group"

# ── XP / Text Level System ────────────────────────────────────────────────────
XP_DATA:      dict[int, dict[int, int]]   = {}   # guild_id -> {user_id -> xp}
XP_COOLDOWN:  dict[int, dict[int, float]] = {}   # guild_id -> {user_id -> last_xp_time}
XP_PER_MSG   = 15
XP_COOLDOWN_SECS = 60

def _xp_to_level(xp: int) -> int:
    level = 0
    while xp >= _xp_required(level + 1):
        xp -= _xp_required(level + 1)
        level += 1
    return level

def _xp_required(level: int) -> int:
    return 100 * (level ** 2) + 50 * level + 100

def _add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int, bool]:
    """Add XP and return (old_level, new_level, leveled_up)."""
    guild_xp = XP_DATA.setdefault(guild_id, {})
    before = guild_xp.get(user_id, 0)
    guild_xp[user_id] = before + amount
    old_lvl = _xp_to_level(before)
    new_lvl = _xp_to_level(guild_xp[user_id])
    return old_lvl, new_lvl, new_lvl > old_lvl

# ── Voice Time Tracking ───────────────────────────────────────────────────────
VOICE_JOIN_TIME: dict[int, dict[int, float]] = {}  # guild_id -> {user_id -> join_timestamp}
VOICE_MINUTES:   dict[int, dict[int, int]]   = {}  # guild_id -> {user_id -> total_minutes}

# ── Invite Tracking ───────────────────────────────────────────────────────────
INVITE_CACHE:  dict[int, dict[str, discord.Invite]] = {}  # guild_id -> {code -> Invite}
INVITE_COUNTS: dict[int, dict[int, int]]            = {}  # guild_id -> {inviter_id -> count}

# ── Tickets ───────────────────────────────────────────────────────────────────
OPEN_TICKETS: dict[int, int] = {}  # user_id -> channel_id
TICKET_CATEGORY_NAME = "Support Tickets"
TICKET_LOG_NAME      = "ticket-logs"
_TICKET_LOCKS: dict[int, asyncio.Lock] = {}  # per-user lock prevents race condition duplicates

def _get_ticket_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _TICKET_LOCKS:
        _TICKET_LOCKS[user_id] = asyncio.Lock()
    return _TICKET_LOCKS[user_id]

# ── Reaction Roles ────────────────────────────────────────────────────────────
# message_id -> {emoji_str -> role_id}
REACTION_ROLES: dict[int, dict[str, int]] = {}

# ── Giveaways ─────────────────────────────────────────────────────────────────
# message_id -> {channel_id, end_time, prize, winners, host_id, ended}
GIVEAWAYS: dict[int, dict] = {}

# ── Streamer Alerts ───────────────────────────────────────────────────────────
# list of {"name": str, "platform": "twitch"|"youtube", "last_live": bool}
STREAMERS:      list[dict] = []
STREAMER_CHANNEL_NAME = "streamer-alerts"

# ── Free Games ────────────────────────────────────────────────────────────────
FREE_GAMES_CHANNEL_NAME = "free-games"
_FREE_GAMES_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posted_free_games.json")
POSTED_FREE_GAMES: set[int] = set()  # app IDs already announced

def _load_posted_games() -> None:
    """Load previously posted game IDs from disk so we never repost them."""
    global POSTED_FREE_GAMES
    if not os.path.exists(_FREE_GAMES_SAVE):
        return
    try:
        with open(_FREE_GAMES_SAVE, "r", encoding="utf-8") as f:
            POSTED_FREE_GAMES = set(json.load(f))
    except Exception as e:
        print(f"[FreeGames] Warning: could not load posted games list — {e}")

def _save_posted_games() -> None:
    """Persist posted game IDs to disk."""
    try:
        with open(_FREE_GAMES_SAVE, "w", encoding="utf-8") as f:
            json.dump(list(POSTED_FREE_GAMES), f)
    except Exception as e:
        print(f"[FreeGames] Warning: could not save posted games list — {e}")

_load_posted_games()

# ── Personal Space (private temp voice channels) ──────────────────────────────
# guild_id -> lobby voice channel ID that triggers creation
PERSONAL_SPACE_LOBBY: dict[int, int] = {}
# channel_id -> owner member_id  (tracks active personal space channels)
PERSONAL_SPACE_CHANNELS: dict[int, int] = {}

# ── Game Channel System ───────────────────────────────────────────────────────
GAME_LIST = [
    ("Arc Raiders",     "🔫"),
    ("Fortnite",         "🏅"),
    ("Phasmophobia",     "👻"),
    ("Minecraft",        "⛏️"),
    ("Valorant",         "🎯"),
    ("Call of Duty",     "💀"),
    ("Apex Legends",     "🦾"),
    ("Roblox",           "🟥"),
    ("GTA V",            "🚗"),
    ("Rocket League",    "🚀"),
    ("Warzone",          "🪖"),
    ("EA FC",            "⚽"),
    ("Overwatch 2",      "🔱"),
    ("League of Legends","🧙"),
    ("CS2",              "💣"),
]

# ── Ticket System ─────────────────────────────────────────────────────────────
class TicketCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close", emoji="🔒")

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not interaction.user.guild_permissions.manage_channels and \
           OPEN_TICKETS.get(interaction.user.id) != ch.id:
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Closing ticket in 5 seconds...")
        # log transcript
        log_ch = discord.utils.get(interaction.guild.text_channels, name=TICKET_LOG_NAME)
        if log_ch:
            msgs = [m async for m in ch.history(limit=200, oldest_first=True)]
            transcript = "\n".join(
                f"[{m.created_at.strftime('%H:%M:%S')}] {m.author}: {m.content}"
                for m in msgs if not m.author.bot or m.content
            )
            embed = discord.Embed(title=f"📋 Ticket Closed — #{ch.name}", color=0xE74C3C)
            embed.description = f"```\n{transcript[:3900]}\n```" if transcript else "*No messages.*"
            embed.timestamp = discord.utils.utcnow()
            await log_ch.send(embed=embed)
        # remove from open tickets
        for uid, cid in list(OPEN_TICKETS.items()):
            if cid == ch.id:
                del OPEN_TICKETS[uid]
                break
        await asyncio.sleep(5)
        await ch.delete(reason="Ticket closed")

# ── Gamer Verification Button ───────────────────────────────────────────────────────────────
class GamerVerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Verify — Get Access",
            style=discord.ButtonStyle.success,
            custom_id="gamer_verify",
            emoji="✅",
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(interaction.user.id)
        gamer_role = discord.utils.get(interaction.guild.roles, name=GAMER_ROLE_NAME)
        if not gamer_role:
            await interaction.response.send_message(
                "❌ The Gamer role doesn't exist yet — ask an admin to run `/setupchannels`.",
                ephemeral=True,
            )
            return
        if gamer_role in member.roles:
            await interaction.response.send_message(
                "✅ You're already verified and have full access!", ephemeral=True
            )
            return
        await member.add_roles(gamer_role, reason="Self-verification via embed button")
        await log_role_change(interaction.guild, member, gamer_role, added=True, source="Verification Embed")
        await interaction.response.send_message(
            f"🎉 Welcome! You now have the **@{GAMER_ROLE_NAME}** role and can access all channels!",
            ephemeral=True,
        )

class GamerVerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(GamerVerifyButton())


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCloseButton())

class OpenTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Open a Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_open", emoji="🎫")

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        guild  = interaction.guild
        lock   = _get_ticket_lock(member.id)

        # Defer immediately to prevent Discord timeout while we acquire the lock
        await interaction.response.defer(ephemeral=True)

        async with lock:
            # Check in-memory tracking first
            if member.id in OPEN_TICKETS:
                ch = guild.get_channel(OPEN_TICKETS[member.id])
                if ch:
                    await interaction.followup.send(f"You already have an open ticket: {ch.mention}", ephemeral=True)
                    return
                else:
                    # Channel was deleted without going through close flow — clean up
                    del OPEN_TICKETS[member.id]

            # Also scan the actual category in case the bot restarted and lost memory
            cat = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
            if cat:
                existing = discord.utils.get(cat.text_channels, name=f"ticket-{member.name}")
                if existing:
                    OPEN_TICKETS[member.id] = existing.id
                    await interaction.followup.send(f"You already have an open ticket: {existing.mention}", ephemeral=True)
                    return
            else:
                cat = await guild.create_category(TICKET_CATEGORY_NAME)

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            # give mods access too
            for role in guild.roles:
                if role.permissions.manage_channels:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            ch = await guild.create_text_channel(
                f"ticket-{member.name}",
                category=cat,
                overwrites=overwrites,
                reason="Support ticket",
            )
            OPEN_TICKETS[member.id] = ch.id
            embed = discord.Embed(
                title="🎫 Support Ticket",
                description=f"Hello {member.mention}! Describe your issue and a staff member will assist you.\n\nClick **Close Ticket** when resolved.",
                color=0x5865F2,
            )
            embed.set_footer(text="Do not share personal information.")
            await ch.send(embed=embed, view=TicketView())
            await interaction.followup.send(f"🎫 Ticket created: {ch.mention}", ephemeral=True)

class OpenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OpenTicketButton())

# ── Background Tasks ──────────────────────────────────────────────────────────
@tasks.loop(minutes=2)
async def empty_vc_cleanup():
    """Scan all guilds for empty user-created voice channels and delete them."""
    for guild in client.guilds:
        # Channels to always keep: lobby triggers, AFK channel, channels with 0 user limit that are permanent
        protected_ids: set[int] = set()
        if guild.afk_channel:
            protected_ids.add(guild.afk_channel.id)
        # Protect all Personal Space lobby channels
        lobby_id = PERSONAL_SPACE_LOBBY.get(guild.id)
        if lobby_id:
            protected_ids.add(lobby_id)
        # Protect all known music / permanent VCs by name keywords
        permanent_keywords = ("music", "stream", "stage", "afk", "join to create", "general", "lounge", "waiting")
        for vc in guild.voice_channels:
            if any(kw in vc.name.lower() for kw in permanent_keywords):
                protected_ids.add(vc.id)
        for vc in guild.voice_channels:
            if vc.id in protected_ids:
                continue
            if len(vc.members) == 0:
                # Only delete channels that were explicitly created by the Personal Space system
                if vc.id in PERSONAL_SPACE_CHANNELS:
                    try:
                        PERSONAL_SPACE_CHANNELS.pop(vc.id, None)
                        await vc.delete(reason="Empty Personal Space channel — auto-cleaned")
                        print(f"[VCCleanup] Deleted empty Personal Space channel: #{vc.name} in {guild.name}")
                    except Exception as e:
                        print(f"[VCCleanup] Could not delete #{vc.name}: {e}")

@tasks.loop(seconds=30)
async def giveaway_check():
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for msg_id, gdata in list(GIVEAWAYS.items()):
        if gdata["ended"] or now < gdata["end_time"]:
            continue
        gdata["ended"] = True
        guild = client.get_guild(GUILD_ID.id)
        if not guild:
            continue
        ch = guild.get_channel(gdata["channel_id"])
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(msg_id)
        except Exception:
            continue
        # collect users who reacted 🎉
        winners_list = []
        for reaction in msg.reactions:
            if str(reaction.emoji) == "🎉":
                async for user in reaction.users():
                    if not user.bot:
                        winners_list.append(user)
                break
        count = gdata["winners"]
        if winners_list:
            picked = random.sample(winners_list, min(count, len(winners_list)))
            winner_mentions = " ".join(w.mention for w in picked)
            await ch.send(
                f"🎉 **Giveaway ended!** Congratulations to {winner_mentions}!\n"
                f"Prize: **{gdata['prize']}**"
            )
        else:
            await ch.send(f"🎉 **Giveaway ended!** No valid entries for **{gdata['prize']}**.")

@tasks.loop(minutes=5)
async def streamer_check():
    if not STREAMERS:
        return
    guild = client.get_guild(GUILD_ID.id)
    if not guild:
        return
    alert_ch = discord.utils.get(guild.text_channels, name=STREAMER_CHANNEL_NAME)
    if not alert_ch:
        return
    for streamer in STREAMERS:
        name     = streamer["name"]
        platform = streamer["platform"]
        try:
            if platform == "twitch":
                url = f"https://www.twitch.tv/{name}"
                req = urllib.request.Request(
                    f"https://api.twitch.tv/helix/streams?user_login={name}",
                    headers={"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko", "Authorization": "Bearer invalid"}
                )
                # Simplified: check via page scrape keyword
                req2 = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req2, timeout=5) as r:
                    content = r.read().decode(errors="ignore")
                is_live = '"isLiveBroadcast"' in content
            else:
                is_live = False  # YouTube live detection requires API key — placeholder
        except Exception:
            continue

        was_live = streamer.get("last_live", False)
        streamer["last_live"] = is_live
        if is_live and not was_live:
            embed = discord.Embed(
                title=f"🔴 {name} is now LIVE on {platform.title()}!",
                url=f"https://www.twitch.tv/{name}" if platform == "twitch" else f"https://www.youtube.com/@{name}",
                color=0x9146FF if platform == "twitch" else 0xFF0000,
            )
            embed.set_footer(text="Click the title to watch!")
            await alert_ch.send(embed=embed)


def _fetch_steam_free_games() -> list[dict]:
    """Fetch active free game giveaways from GamerPower + Steam specials."""
    results = []
    seen_ids: set = set()

    # ── Source 1: GamerPower API (Steam giveaways) ───────────────────────────
    try:
        gp_url = "https://www.gamerpower.com/api/giveaways?platform=steam&type=game&status=active"
        req = urllib.request.Request(gp_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            gp_data = json.loads(r.read().decode(errors="ignore"))
        if isinstance(gp_data, list):
            for g in gp_data:
                gid = f"gp_{g['id']}"
                if gid in seen_ids:
                    continue
                seen_ids.add(gid)
                # Try to extract a Steam app URL from the giveaway page URL
                steam_url = g.get("open_giveaway_url", g.get("gamerpower_url", ""))
                results.append({
                    "id":           gid,
                    "name":         g.get("title", "Unknown"),
                    "header_image": g.get("image", g.get("thumbnail", "")),
                    "thumbnail":    g.get("thumbnail", ""),
                    "url":          steam_url,
                    "worth":        g.get("worth", "Free"),
                    "description":  g.get("description", ""),
                    "end_date":     g.get("end_date", ""),
                    "platforms":    g.get("platforms", "Steam"),
                    "source":       "gamerpower",
                })
    except Exception as e:
        print(f"[FreeGames] GamerPower error: {e}")

    # ── Source 2: Steam featured specials (100 % off) ────────────────────────
    try:
        url = "https://store.steampowered.com/api/featuredcategories"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode(errors="ignore"))
        for game in data.get("specials", {}).get("items", []):
            if game.get("discount_percent") == 100 and game.get("final_price") == 0:
                gid = f"steam_{game['id']}"
                if gid in seen_ids:
                    continue
                seen_ids.add(gid)
                orig = game.get("original_price", 0)
                results.append({
                    "id":           gid,
                    "name":         game.get("name", "Unknown"),
                    "header_image": game.get("large_capsule_image") or game.get("small_capsule_image", ""),
                    "thumbnail":    game.get("small_capsule_image", ""),
                    "url":          f"https://store.steampowered.com/app/{game['id']}/",
                    "worth":        f"${orig / 100:.2f}" if orig else "Paid",
                    "description":  "",
                    "end_date":     "",
                    "platforms":    "Steam",
                    "source":       "steam",
                })
    except Exception as e:
        print(f"[FreeGames] Steam error: {e}")

    # ── Source 3: Epic Games Store (currently free promotions) ───────────────
    try:
        epic_url = (
            "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
            "?locale=en-US&country=US&allowCountries=US"
        )
        req = urllib.request.Request(epic_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            epic_data = json.loads(r.read().decode(errors="ignore"))
        elements = (
            epic_data.get("data", {})
            .get("Catalog", {})
            .get("searchStore", {})
            .get("elements", [])
        )
        for g in elements:
            promos = g.get("promotions") or {}
            current = promos.get("promotionalOffers", [])
            if not current:
                continue
            # Must have an active offer where discountPercentage == 0 (100% off)
            is_free = False
            for offer_group in current:
                for offer in offer_group.get("promotionalOffers", []):
                    if offer.get("discountSetting", {}).get("discountPercentage", 999) == 0:
                        is_free = True
            if not is_free:
                continue
            slug = g.get("productSlug") or g.get("urlSlug") or ""
            # Some slugs contain '/home' suffix — strip it
            slug = slug.replace("/home", "").strip()
            epic_store_url = f"https://store.epicgames.com/en-US/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games"
            gid = f"epic_{g.get('id', slug)}"
            if gid in seen_ids:
                continue
            seen_ids.add(gid)
            # Key art image
            key_images = g.get("keyImages", [])
            header_img = next((img["url"] for img in key_images if img.get("type") == "DieselStoreFrontWide"), "")
            if not header_img:
                header_img = next((img["url"] for img in key_images if img.get("type") == "OfferImageWide"), "")
            thumb_img = next((img["url"] for img in key_images if img.get("type") == "Thumbnail"), header_img)
            orig_price = g.get("price", {}).get("totalPrice", {}).get("fmtPrice", {}).get("originalPrice", "")
            results.append({
                "id":           gid,
                "name":         g.get("title", "Unknown"),
                "header_image": header_img,
                "thumbnail":    thumb_img,
                "url":          epic_store_url,
                "worth":        orig_price,
                "description":  g.get("description", ""),
                "end_date":     "",
                "platforms":    "Epic Games",
                "source":       "epic",
            })
    except Exception as e:
        print(f"[FreeGames] Epic error: {e}")

    return results


class FreeGameView(discord.ui.View):
    """Persistent view with a clickable store button."""
    def __init__(self, url: str, source: str = "steam"):
        super().__init__(timeout=None)
        if source == "epic":
            label = "🛒 Claim on Epic Games Store"
        elif source == "gamerpower":
            label = "🎮 Claim / View Game"
        else:
            label = "🎮 Claim / View on Steam"
        self.add_item(discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.link,
            url=url,
        ))


def _build_free_game_embed(game: dict) -> discord.Embed:
    """Build a rich embed for a single free game."""
    worth = game.get("worth", "")
    orig_str = f"~~{worth}~~ → **FREE**" if worth and worth != "Free" else "**FREE**"
    end = game.get("end_date", "")
    end_line = f"\n⏰ **Ends:** {end}" if end and end.lower() not in ("n/a", "") else ""
    desc_raw = game.get("description", "")
    desc_snippet = (desc_raw[:200] + "…") if len(desc_raw) > 200 else desc_raw
    embed = discord.Embed(
        title=f"🆓  {game['name']}",
        url=game["url"],
        description=f"**Price:** {orig_str}{end_line}\n\n{desc_snippet}".strip(),
        color=0x1B2838,
    )
    source = game.get("source", "steam")
    color_map = {"epic": 0x2D2D2D, "gamerpower": 0x1B2838, "steam": 0x1B2838}
    embed.color = color_map.get(source, 0x1B2838)
    if source == "epic":
        embed.set_author(name="Epic Games Store", icon_url="https://upload.wikimedia.org/wikipedia/commons/thumb/3/31/Epic_Games_logo.svg/120px-Epic_Games_logo.svg.png")
    else:
        embed.set_author(name="Steam", icon_url="https://store.steampowered.com/favicon.ico")
    if game.get("header_image"):
        embed.set_image(url=game["header_image"])
    if game.get("thumbnail") and game["thumbnail"] != game.get("header_image"):
        embed.set_thumbnail(url=game["thumbnail"])
    source_label = {"epic": "Epic Games Store", "gamerpower": "GamerPower/Steam", "steam": "Steam Store"}.get(source, "Steam")
    embed.set_footer(text=f"Source: {source_label} • Grab it before the offer ends!")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def _post_free_games(ch: discord.TextChannel, games: list[dict]):
    """Post a summary embed followed by individual game embeds with buttons."""
    if not games:
        await ch.send(embed=discord.Embed(
            description="🔍 No free games found right now on Steam or Epic. The bot checks every 4 hours — we'll post automatically when deals appear!",
            color=0x1B2838,
        ))
        return

    # ── Summary embed ────────────────────────────────────────────────────────
    lines = []
    for i, g in enumerate(games, 1):
        store_tag = "[Epic]" if g.get("source") == "epic" else "[Steam]"
        lines.append(f"**{i}.** {store_tag} [{g['name']}]({g['url']})")
    summary = discord.Embed(
        title=f"🎮 {len(games)} Free Game{'s' if len(games) != 1 else ''} Available Right Now!",
        description="\n".join(lines),
        color=0x00C851,
    )
    summary.set_footer(text="Click any title below to go to the store • Updated every 4 hours")
    summary.timestamp = discord.utils.utcnow()
    await ch.send(embed=summary)

    # ── Individual game embeds with Claim button ──────────────────────────────
    for game in games:
        embed = _build_free_game_embed(game)
        view = FreeGameView(game["url"], source=game.get("source", "steam"))
        await ch.send(embed=embed, view=view)


@tasks.loop(hours=4)
async def free_games_check():
    guild = client.get_guild(GUILD_ID.id)
    if not guild:
        return

    # Get or create the free-games channel
    ch = discord.utils.get(guild.text_channels, name=FREE_GAMES_CHANNEL_NAME)
    if not ch:
        try:
            ch = await guild.create_text_channel(
                FREE_GAMES_CHANNEL_NAME,
                topic="🎮 Free games on Steam — auto-updated every 4 hours",
                reason="Free games auto-channel",
            )
            # Post a header message the first time the channel is created
            intro = discord.Embed(
                title="🎮 Free Games on Steam",
                description=(
                    "This channel is automatically updated every **4 hours** "
                    "with games that are currently **100% off** on Steam.\n\n"
                    "Grab them before the offer ends!"
                ),
                color=0x1B2838,
            )
            intro.set_footer(text="Powered by the Steam store API")
            await ch.send(embed=intro)
        except Exception as e:
            print(f"[FreeGames] Could not create channel: {e}")
            return

    loop = asyncio.get_running_loop()
    games = await loop.run_in_executor(None, _fetch_steam_free_games)

    new_games = [g for g in games if g["id"] not in POSTED_FREE_GAMES]
    if new_games:
        for g in new_games:
            POSTED_FREE_GAMES.add(g["id"])
        _save_posted_games()
        await _post_free_games(ch, new_games)
    # else: no new games — don't spam the channel


class GameRoleButton(discord.ui.Button):
    def __init__(self, game: str, emoji: str):
        super().__init__(
            label=game,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"game_role_{game}",
        )
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(interaction.user.id)
        role = discord.utils.get(interaction.guild.roles, name=f"Game: {self.game}")
        if not role:
            await interaction.response.send_message(
                f"Role for **{self.game}** not found. Ask an admin to run `/setupgames`.",
                ephemeral=True
            )
            return
        if role in member.roles:
            await member.remove_roles(role, reason="Game channel toggle")
            await interaction.response.send_message(
                f"🚪 You left **{self.game}** — channels are now hidden.",
                ephemeral=True
            )
            await log_role_change(interaction.guild, member, role, added=False, source="Game Role Button")
        else:
            await member.add_roles(role, reason="Game channel toggle")
            await interaction.response.send_message(
                f"✅ You joined **{self.game}** — channels are now visible!",
                ephemeral=True
            )
            await log_role_change(interaction.guild, member, role, added=True, source="Game Role Button")

class GameRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for game, emoji in GAME_LIST:
            self.add_item(GameRoleButton(game, emoji))

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    await client.process_commands(message)

    # ── XP gain ───────────────────────────────────────────────────────────────
    gid, uid = message.guild.id, message.author.id
    now_f = time.time()
    cooldowns = XP_COOLDOWN.setdefault(gid, {})
    if now_f - cooldowns.get(uid, 0) >= XP_COOLDOWN_SECS:
        cooldowns[uid] = now_f
        old_lvl, new_lvl, leveled_up = _add_xp(gid, uid, XP_PER_MSG)
        if leveled_up:
            await message.channel.send(
                f"🎉 {message.author.mention} leveled up to **Level {new_lvl}**!",
                delete_after=10,
            )

    # ── Auto-moderation ───────────────────────────────────────────────────────
    # Skip whitelisted users and admins
    if message.author.id not in WHITELIST and not message.author.guild_permissions.administrator:
        content_lower = message.content.lower()

        # Banned word filter
        if any(word in content_lower for word in BANNED_WORDS):
            try:
                await message.delete()
            except discord.HTTPException:
                pass

            uid = message.author.id
            BANNED_WORD_WARNINGS[uid] = BANNED_WORD_WARNINGS.get(uid, 0) + 1
            warn_count = BANNED_WORD_WARNINGS[uid]

            if warn_count >= 2:
                # Second offense — ban
                BANNED_WORD_WARNINGS.pop(uid, None)
                notice = await message.channel.send(
                    f"{message.author.mention} You have been **banned** for repeated use of banned words."
                )
                await asyncio.sleep(5)
                try:
                    await notice.delete()
                except discord.HTTPException:
                    pass
                try:
                    await message.author.ban(reason="Auto-mod: repeated banned word usage (2nd offense)")
                except discord.HTTPException:
                    pass
                log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
                if log_ch:
                    embed = discord.Embed(title="🔨 Auto-Mod: Banned (2nd Offense)", color=0x992D22)
                    embed.add_field(name="User", value=f"{message.author.mention} ({message.author})", inline=False)
                    embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                    embed.add_field(name="Reason", value="Repeated banned word usage", inline=False)
                    embed.timestamp = discord.utils.utcnow()
                    await log_ch.send(embed=embed)
            else:
                # First offense — warning
                warning = await message.channel.send(
                    f"{message.author.mention} ⚠️ **Warning 1/2:** Your message was removed for containing a banned word. "
                    f"A second offense will result in a **ban**."
                )
                await asyncio.sleep(7)
                try:
                    await warning.delete()
                except discord.HTTPException:
                    pass
                log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
                if log_ch:
                    embed = discord.Embed(title="🚫 Auto-Mod: Banned Word (Warning 1/2)", color=0xE74C3C)
                    embed.add_field(name="User", value=f"{message.author.mention} ({message.author})", inline=False)
                    embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                    embed.timestamp = discord.utils.utcnow()
                    await log_ch.send(embed=embed)
            return

        # Spam detection
        now_ts = discord.utils.utcnow().timestamp()
        tracker = spam_tracker.setdefault(message.author.id, [])
        tracker.append(now_ts)
        spam_tracker[message.author.id] = [t for t in tracker if now_ts - t < SPAM_WINDOW]
        if len(spam_tracker[message.author.id]) >= SPAM_THRESHOLD:
            spam_tracker[message.author.id] = []
            try:
                await message.author.timeout(datetime.timedelta(minutes=2), reason="Auto-mod: spamming")
            except discord.HTTPException:
                pass
            warning = await message.channel.send(
                f"{message.author.mention} You have been timed out for 2 minutes for spamming."
            )
            await asyncio.sleep(5)
            try:
                await warning.delete()
            except discord.HTTPException:
                pass
            log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
            if log_ch:
                embed = discord.Embed(title="⏱️ Auto-Mod: Spam Timeout", color=0xE67E22)
                embed.add_field(name="User", value=f"{message.author.mention} ({message.author})", inline=False)
                embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                embed.add_field(name="Duration", value="2 minutes", inline=False)
                embed.timestamp = discord.utils.utcnow()
                await log_ch.send(embed=embed)
            return

        # ── Discord/Bot advertisement detection ───────────────────────────────
        _AD_PATTERNS = [
            r"discord\.gg/\S+",                     # discord.gg/invite
            r"discord\.com/invite/\S+",             # discord.com/invite/...
            r"discordapp\.com/invite/\S+",          # old format
            r"dsc\.gg/\S+",                         # shortener
            r"top\.gg/bot/\d+",                     # bot listing
            r"discord\.me/\S+",                     # discord.me
            r"disboard\.org/server/\S+",            # disboard
        ]
        is_ad = any(re.search(pat, message.content, re.IGNORECASE) for pat in _AD_PATTERNS)

        if is_ad:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

            uid = message.author.id
            AD_WARNINGS[uid] = AD_WARNINGS.get(uid, 0) + 1
            warn_count = AD_WARNINGS[uid]

            log_ch = message.guild.get_channel(LOG_CHANNEL_ID)

            if warn_count >= 2:
                # 2nd offense — kick
                AD_WARNINGS.pop(uid, None)
                notice = await message.channel.send(
                    f"{message.author.mention} 🦵 You have been **kicked** for advertising another Discord server or bot. "
                    f"Advertising is not allowed in this server."
                )
                await asyncio.sleep(5)
                try:
                    await notice.delete()
                except discord.HTTPException:
                    pass
                try:
                    await message.author.kick(reason="Auto-mod: advertising (2nd offense)")
                except discord.HTTPException:
                    pass
                if log_ch:
                    embed = discord.Embed(title="🦵 Auto-Mod: Kicked for Advertising (2nd Offense)", color=0xE67E22)
                    embed.add_field(name="User",    value=f"{message.author.mention} ({message.author})", inline=False)
                    embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                    embed.add_field(name="Content", value=message.content[:500], inline=False)
                    embed.add_field(name="Reason",  value="Advertising another server or bot (2nd offense)", inline=False)
                    embed.timestamp = discord.utils.utcnow()
                    await log_ch.send(embed=embed)
            else:
                # 1st offense — 1 minute timeout
                try:
                    await message.author.timeout(
                        datetime.timedelta(minutes=1),
                        reason="Auto-mod: advertising another Discord server/bot (1st offense)",
                    )
                except discord.HTTPException:
                    pass
                warning = await message.channel.send(
                    f"{message.author.mention} ⚠️ **Warning 1/2:** Advertising other Discord servers or bots is not allowed.\n"
                    f"You have been timed out for **1 minute**. A second offense will result in a **kick**."
                )
                await asyncio.sleep(8)
                try:
                    await warning.delete()
                except discord.HTTPException:
                    pass
                if log_ch:
                    embed = discord.Embed(title="⏱️ Auto-Mod: Ad Timeout (Warning 1/2)", color=0xFFAA00)
                    embed.add_field(name="User",     value=f"{message.author.mention} ({message.author})", inline=False)
                    embed.add_field(name="Channel",  value=message.channel.mention, inline=False)
                    embed.add_field(name="Content",  value=message.content[:500], inline=False)
                    embed.add_field(name="Duration", value="1 minute timeout", inline=False)
                    embed.timestamp = discord.utils.utcnow()
                    await log_ch.send(embed=embed)
            return



async def get_mod_log_channel(guild: discord.Guild):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        print(f"Log channel with ID {LOG_CHANNEL_ID} not found.")
    return channel

async def log_action(interaction: discord.Interaction, action: str, target: str, reason: str = None):
    # Send to legacy LOG_CHANNEL_ID if present
    channel = await get_mod_log_channel(interaction.guild)
    description = f"**Action:** {action}\n**Target:** {target}\n**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)"
    if reason:
        description += f"\n**Reason:** {reason}"
    embed = discord.Embed(title="🔨 Mod Action", description=description, color=0xFF4444)
    embed.set_footer(text=f"Channel: #{interaction.channel.name}")
    embed.timestamp = discord.utils.utcnow()
    if channel:
        await channel.send(embed=embed)
    # Also send to the private mod-logs channel
    mod_log = discord.utils.get(interaction.guild.text_channels, name=MOD_LOG_NAME)
    if mod_log and mod_log != channel:
        await mod_log.send(embed=embed)


async def log_role_change(guild: discord.Guild, member: discord.Member,
                          role: discord.Role, added: bool, source: str) -> None:
    """Log a role add/remove to the private #role-logs channel."""
    ch = discord.utils.get(guild.text_channels, name=ROLE_LOG_NAME)
    if not ch:
        return
    color  = 0x2ECC71 if added else 0xE74C3C
    action = "➕ Role Added" if added else "➖ Role Removed"
    embed  = discord.Embed(title=f"🏷️ {action}", color=color)
    embed.add_field(name="Member",  value=f"{member.mention} (`{member.id}`)",  inline=True)
    embed.add_field(name="Role",    value=f"{role.mention} (`{role.name}`)",    inline=True)
    embed.add_field(name="Source",  value=source,                                inline=True)
    embed.timestamp = discord.utils.utcnow()
    await ch.send(embed=embed)


async def _log_admin_cmd(interaction: discord.Interaction, cmd: str, details: str = "") -> None:
    """Log any admin-only command to the private #mod-logs channel."""
    ch = discord.utils.get(interaction.guild.text_channels, name=MOD_LOG_NAME)
    if not ch:
        return
    embed = discord.Embed(title=f"⚙️ Admin Command: `/{cmd}`", color=0x5865F2)
    embed.add_field(name="Admin",   value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
    embed.add_field(name="Channel", value=f"<#{interaction.channel_id}>",                          inline=True)
    if details:
        embed.add_field(name="Details", value=details, inline=False)
    embed.timestamp = discord.utils.utcnow()
    await ch.send(embed=embed)


@client.tree.command(name="ban", description="Ban a server member", guild=GUILD_ID)
@app_commands.default_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You don't have permission to ban members.", ephemeral=True)
        return
    if user.id in WHITELIST:
        await interaction.response.send_message(f"{user} is whitelisted and cannot be banned.", ephemeral=True)
        return
    await user.ban(reason=reason)
    await interaction.response.send_message(f'Banned {user} for: {reason}')
    await log_action(interaction, "Ban", f"{user} ({user.id})", reason)

@client.tree.command(name="unban", description="Unban a user by their ID", guild=GUILD_ID)
@app_commands.default_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You don't have permission to unban members.", ephemeral=True)
        return
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("Invalid user ID — must be a number.", ephemeral=True)
        return
    try:
        ban_entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
    except discord.NotFound:
        await interaction.response.send_message(f"No ban found for user ID `{uid}`.", ephemeral=True)
        return
    await interaction.guild.unban(ban_entry.user, reason=reason)
    await interaction.response.send_message(f"✅ Unbanned **{ban_entry.user}** (`{uid}`) — {reason}")
    await log_action(interaction, "Unban", f"{ban_entry.user} ({uid})", reason)

@client.tree.command(name="banlist", description="Show all currently banned users", guild=GUILD_ID)
@app_commands.default_permissions(ban_members=True)
async def banlist(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You don't have permission to view the ban list.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    bans = [entry async for entry in interaction.guild.bans()]
    if not bans:
        await interaction.followup.send("No users are currently banned.", ephemeral=True)
        return
    # Paginate at 20 per embed to avoid hitting field limits
    entries_per_page = 20
    pages = [bans[i:i + entries_per_page] for i in range(0, len(bans), entries_per_page)]
    embeds = []
    for page_num, page in enumerate(pages, 1):
        embed = discord.Embed(
            title=f"🔨 Ban List — {len(bans)} banned user{'s' if len(bans) != 1 else ''}",
            color=0xE74C3C,
        )
        if len(pages) > 1:
            embed.set_footer(text=f"Page {page_num}/{len(pages)}")
        lines = [f"`{entry.user.id}` **{entry.user}**" + (f" — {entry.reason}" if entry.reason else "") for entry in page]
        embed.description = "\n".join(lines)
        embeds.append(embed)
    for embed in embeds:
        await interaction.followup.send(embed=embed, ephemeral=True)

@client.tree.command(name="kick", description="Kick a server member", guild=GUILD_ID)
@app_commands.default_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("You don't have permission to kick members.", ephemeral=True)
        return
    if user.id in WHITELIST:
        await interaction.response.send_message(f"{user} is whitelisted and cannot be kicked.", ephemeral=True)
        return
    await user.kick(reason=reason)
    await interaction.response.send_message(f'Kicked {user} for: {reason}')
    await log_action(interaction, "Kick", f"{user} ({user.id})", reason)

@client.tree.command(name="mute", description="Mute a member for a number of seconds", guild=GUILD_ID)
@app_commands.default_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, duration: int = 60):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to mute members.", ephemeral=True)
        return
    if user.id in WHITELIST:
        await interaction.response.send_message(f"{user} is whitelisted and cannot be muted.", ephemeral=True)
        return
    await user.timeout(timedelta(seconds=duration))
    await interaction.response.send_message(f'Muted {user} for {duration} seconds.')
    await log_action(interaction, "Mute", f"{user} ({user.id})", f"Duration: {duration} seconds")

@client.tree.command(name="unmute", description="Unmute a member", guild=GUILD_ID)
@app_commands.default_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to unmute members.", ephemeral=True)
        return
    await user.timeout(None)
    await interaction.response.send_message(f'Unmuted {user}.')
    await log_action(interaction, "Unmute", f"{user} ({user.id})")

@client.tree.command(name="timeout", description="Timeout a member for a set number of hours and minutes", guild=GUILD_ID)
@app_commands.default_permissions(moderate_members=True)
async def timeout_member(interaction: discord.Interaction, user: discord.Member, hours: int = 0, minutes: int = 0, reason: str = "No reason provided"):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to timeout members.", ephemeral=True)
        return
    if user.id in WHITELIST:
        await interaction.response.send_message(f"{user} is whitelisted and cannot be timed out.", ephemeral=True)
        return
    total_seconds = hours * 3600 + minutes * 60
    if total_seconds <= 0:
        await interaction.response.send_message("Please provide a duration greater than 0.", ephemeral=True)
        return
    await user.timeout(timedelta(seconds=total_seconds), reason=reason)
    await interaction.response.send_message(f'Timed out {user} for {hours}h {minutes}m.')
    await log_action(interaction, "Timeout", f"{user} ({user.id})", f"{hours}h {minutes}m — {reason}")

@client.tree.command(name="whitelist", description="Whitelist a member to protect them from mod actions", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def whitelist(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You don't have permission to whitelist members.", ephemeral=True)
        return
    WHITELIST.add(user.id)
    await interaction.response.send_message(f'{user} has been whitelisted.')
    await log_action(interaction, "Whitelist", f"{user} ({user.id})")

@client.tree.command(name="unwhitelist", description="Remove a member from the whitelist", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def unwhitelist(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You don't have permission to manage the whitelist.", ephemeral=True)
        return
    WHITELIST.discard(user.id)
    await interaction.response.send_message(f'{user} has been removed from the whitelist.')
    await log_action(interaction, "Unwhitelist", f"{user} ({user.id})")

@client.tree.command(name="clear", description="Clear messages from the channel", guild=GUILD_ID)
@app_commands.default_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: int = 10):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You don't have permission to manage messages.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount) # pyright: ignore[reportAttributeAccessIssue]
    await interaction.followup.send(f'Cleared {len(deleted)} messages.', ephemeral=True)
    await log_action(interaction, "Clear", f"Channel: {interaction.channel.name}", f"Deleted: {len(deleted)} messages")

# ── Music System ─────────────────────────────────────────────────────────────

from pytubefix import Search, YouTube as PyTube

import platform as _platform
import shutil as _shutil
def _resolve_ffmpeg_executable() -> str:
    if _platform.system() == "Windows":
        return (
            r"C:\Users\kobec\AppData\Local\Microsoft\WinGet\Packages"
            r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            r"\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
        )

    system_ffmpeg = _shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
        bundled_ffmpeg = _shutil.which("ffmpeg")
        if bundled_ffmpeg:
            return bundled_ffmpeg
    except Exception as e:
        print(f"[Music] static-ffmpeg fallback unavailable: {e}")

    return "ffmpeg"

if _platform.system() == "Windows":
    FFMPEG_EXE = _resolve_ffmpeg_executable()
else:
    FFMPEG_EXE = _resolve_ffmpeg_executable()
print(f"[Music] Using FFmpeg executable: {FFMPEG_EXE}")

FFMPEG_OPTS = {
    'executable': FFMPEG_EXE,
    'before_options': '-nostdin -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -q:a 5 -ac 2 -ar 48000',
}

class SongEntry:
    def __init__(self, title: str, url: str, webpage_url: str, duration: int, requester: discord.Member, local_path: str | None = None):
        self.title = title
        self.url = url
        self.webpage_url = webpage_url
        self.duration = duration
        self.requester = requester
        self.local_path = local_path

    def format_duration(self) -> str:
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

class GuildMusicState:
    def __init__(self):
        self.queue: collections.deque[SongEntry] = collections.deque()
        self.current: SongEntry | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.volume: float = 0.5
        self.now_playing_msg: discord.Message | None = None
        self.autoplay: bool = False

music_states: dict[int, GuildMusicState] = {}

def get_music_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]

def _pytubefix_search(query: str, max_results: int) -> list[dict]:
    results = Search(query)
    entries = []
    temp_dir = os.path.join(tempfile.gettempdir(), "gzbot_audio")
    os.makedirs(temp_dir, exist_ok=True)
    for yt in results.videos[:max_results]:
        try:
            stream = yt.streams.filter(only_audio=True).order_by('abr').last()
            if stream:
                local_path = stream.download(output_path=temp_dir, skip_existing=False)
                entries.append({
                    'title': yt.title,
                    'url': stream.url,
                    'webpage_url': yt.watch_url,
                    'duration': yt.length or 0,
                    'local_path': local_path,
                })
        except Exception:
            continue
    return entries

def _cleanup_song_file(song: SongEntry | None) -> None:
    if not song or not song.local_path:
        return
    try:
        if os.path.exists(song.local_path):
            os.remove(song.local_path)
    except Exception as e:
        print(f"[Music] Could not remove temp audio file: {e}")

def _yt_suggestions(query: str) -> list[str]:
    """Fetch YouTube search autocomplete suggestions via the public Google suggest API."""
    url = (
        'https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q='
        + urllib.parse.quote(query)
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=3) as resp:
        data = json.loads(resp.read().decode())
    return [item for item in data[1] if isinstance(item, str)][:8]

async def search_youtube(query: str, max_results: int = 1) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _pytubefix_search(query, max_results))

def play_next(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    if state.queue and state.voice_client and state.voice_client.is_connected():
        state.current = state.queue.popleft()
        input_source = state.current.local_path or state.current.url
        source = discord.FFmpegOpusAudio(
            input_source,
            executable=FFMPEG_EXE,
            before_options=FFMPEG_OPTS['before_options'],
            options=f"-vn -af volume={state.volume}",
        )
        def after_play(error):
            if error:
                print(f'[Music] Player error: {error}')
            finished_song = state.current
            _cleanup_song_file(finished_song)
            asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)
        state.voice_client.play(source, after=after_play)
    else:
        _cleanup_song_file(state.current)
        state.current = None

async def play_next_async(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    # If queue is empty and autoplay is on, fetch a related song
    if not state.queue and state.autoplay and state.current:
        try:
            related = await search_youtube(state.current.title, max_results=2)
            # Skip the first result if it's the same title
            for r in related:
                if r['title'].lower() != state.current.title.lower():
                    state.queue.append(SongEntry(
                        title=r['title'],
                        url=r['url'],
                        webpage_url=r['webpage_url'],
                        duration=r['duration'],
                        requester=state.current.requester,
                    ))
                    break
        except Exception:
            pass
    play_next(guild_id, loop)
    await _post_music_panel(guild_id)

async def search_autocomplete(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
    if not current or len(current) < 2:
        return []
    try:
        loop = asyncio.get_running_loop()
        suggestions = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _yt_suggestions(current)),
            timeout=2.5
        )
        return [
            discord.app_commands.Choice(name=s[:100], value=s[:100])
            for s in suggestions
        ][:25]
    except Exception:
        return []

MUSIC_CHANNEL_NAME = "🎵┃music-commands"


class MusicControlView(discord.ui.View):
    """Persistent music control panel posted in #music-channel."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(emoji="⏸️", label="Pause/Resume", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return
        state.queue.clear()
        state.current = None
        vc.stop()
        await interaction.response.send_message("⏹️ Stopped and queue cleared.", ephemeral=True)

    @discord.ui.button(emoji="📋", label="Queue", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        if not state.current and not state.queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        desc = ""
        if state.current:
            desc += f"**Now Playing:** [{state.current.title}]({state.current.webpage_url}) `{state.current.format_duration()}`\n\n"
        for i, entry in enumerate(state.queue, 1):
            desc += f"`{i}.` [{entry.title}]({entry.webpage_url}) `{entry.format_duration()}`\n"
            if i >= 10:
                remaining = len(state.queue) - 10
                if remaining:
                    desc += f"*...and {remaining} more*"
                break
        embed = discord.Embed(title="📋 Music Queue", description=desc, color=0x9B59B6)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="🔉", label="Vol -10%", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.volume = max(0.0, state.volume - 0.1)
        await interaction.response.send_message(
            f"🔉 Volume: {int(state.volume * 100)}% (applies immediately to next track)",
            ephemeral=True,
        )

    @discord.ui.button(emoji="🔊", label="Vol +10%", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.volume = min(1.0, state.volume + 0.1)
        await interaction.response.send_message(
            f"🔊 Volume: {int(state.volume * 100)}% (applies immediately to next track)",
            ephemeral=True,
        )

    @discord.ui.button(emoji="🔁", label="Autoplay: OFF", style=discord.ButtonStyle.secondary, row=2, custom_id="autoplay_toggle")
    async def autoplay_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.autoplay = not state.autoplay
        button.label = f"Autoplay: {'ON' if state.autoplay else 'OFF'}"
        button.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"🔁 Autoplay is now **{'ON' if state.autoplay else 'OFF'}**. {'I\'ll queue related songs automatically!' if state.autoplay else ''}",
            ephemeral=True
        )


async def _post_music_panel(guild_id: int):
    """Delete the old Now Playing panel and post a fresh one with control buttons."""
    state = get_music_state(guild_id)
    guild = client.get_guild(guild_id)
    if not guild:
        return
    music_ch = discord.utils.get(guild.text_channels, name=MUSIC_CHANNEL_NAME)
    if not music_ch:
        return
    # Delete previous panel
    if state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None
    if not state.current:
        return
    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"[{state.current.title}]({state.current.webpage_url})",
        color=0x1DB954,
    )
    embed.add_field(name="Duration", value=state.current.format_duration())
    embed.add_field(name="Requested by", value=state.current.requester.mention)
    embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%")
    q = len(state.queue)
    footer_parts = []
    if q:
        footer_parts.append(f"{q} song{'s' if q != 1 else ''} in queue")
    if state.autoplay:
        footer_parts.append("🔁 Autoplay ON")
    if footer_parts:
        embed.set_footer(text="  •  ".join(footer_parts))
    view = MusicControlView(guild_id)
    # Reflect autoplay state on the button
    for child in view.children:
        if getattr(child, "custom_id", None) == "autoplay_toggle":
            child.label = f"Autoplay: {'ON' if state.autoplay else 'OFF'}"
            child.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
    state.now_playing_msg = await music_ch.send(embed=embed, view=view)


def _is_music_channel(interaction: discord.Interaction) -> bool:
    """Returns True if the interaction is in the designated music channel."""
    ch = discord.utils.get(interaction.guild.text_channels, name=MUSIC_CHANNEL_NAME)
    return interaction.channel.id == (ch.id if ch else -1)

async def _require_music_channel(interaction: discord.Interaction) -> bool:
    """Sends an error and returns False if not in music-channel."""
    if not _is_music_channel(interaction):
        ch = discord.utils.get(interaction.guild.text_channels, name=MUSIC_CHANNEL_NAME)
        mention = ch.mention if ch else f"#{MUSIC_CHANNEL_NAME}"
        await interaction.response.send_message(
            f"Music commands can only be used in {mention}.", ephemeral=True
        )
        return False
    return True

@client.tree.command(name="play", description="Search and play a song in your voice channel", guild=GUILD_ID)
@discord.app_commands.autocomplete(query=search_autocomplete)
async def play(interaction: discord.Interaction, query: str):
    if not await _require_music_channel(interaction):
        return
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice or not member.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        state = get_music_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await member.voice.channel.connect()
            state.voice_client = vc
        elif vc.channel != member.voice.channel:
            await vc.move_to(member.voice.channel)
            state.voice_client = vc
        else:
            state.voice_client = vc

        results = await search_youtube(query, max_results=1)
        if not results:
            await interaction.followup.send("No results found.")
            return
        r = results[0]
        entry = SongEntry(
            title=r.get('title', 'Unknown'),
            url=r['url'],
            webpage_url=r.get('webpage_url', ''),
            duration=r.get('duration', 0),
            requester=interaction.user,
            local_path=r.get('local_path'),
        )
        state.queue.append(entry)
        if not vc.is_playing() and not vc.is_paused():
            play_next(interaction.guild.id, asyncio.get_running_loop())
            await interaction.followup.send(f"▶️ Starting **{entry.title}** — see the player below!", ephemeral=True)
            await _post_music_panel(interaction.guild.id)
        else:
            embed = discord.Embed(title="Added to Queue", description=f"[{entry.title}]({entry.webpage_url})", color=0x3498DB)
            embed.add_field(name="Position", value=len(state.queue))
            embed.add_field(name="Duration", value=entry.format_duration())
            await interaction.followup.send(embed=embed)
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[Play Error] {tb}')
        await interaction.followup.send(f"Error: {type(e).__name__}: {e}")

@client.tree.command(name="skip", description="Skip the current song", guild=GUILD_ID)
async def skip(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    vc.stop()
    await interaction.response.send_message("Skipped.")

@client.tree.command(name="pause", description="Pause the current song", guild=GUILD_ID)
async def pause(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    vc.pause()
    await interaction.response.send_message("Paused.")

@client.tree.command(name="resume", description="Resume the paused song", guild=GUILD_ID)
async def resume(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        return
    vc.resume()
    await interaction.response.send_message("Resumed.")

@client.tree.command(name="stop", description="Stop playback and clear the queue", guild=GUILD_ID)
async def stop(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    state = get_music_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
        return
    state.queue.clear()
    state.current = None
    vc.stop()
    await interaction.response.send_message("Stopped and queue cleared.")

@client.tree.command(name="leave", description="Disconnect the bot from the voice channel", guild=GUILD_ID)
async def leave(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    state = get_music_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
        return
    state.queue.clear()
    state.current = None
    await vc.disconnect()
    await interaction.response.send_message("Disconnected.")

@client.tree.command(name="queue", description="Show the current music queue", guild=GUILD_ID)
async def queue_cmd(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    state = get_music_state(interaction.guild.id)
    if not state.current and not state.queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    desc = ""
    if state.current:
        desc += f"**Now Playing:** [{state.current.title}]({state.current.webpage_url}) `{state.current.format_duration()}` \u2014 {state.current.requester.mention}\n\n"
    for i, entry in enumerate(state.queue, 1):
        desc += f"`{i}.` [{entry.title}]({entry.webpage_url}) `{entry.format_duration()}` \u2014 {entry.requester.mention}\n"
        if i >= 10:
            remaining = len(state.queue) - 10
            if remaining > 0:
                desc += f"*...and {remaining} more*"
            break
    embed = discord.Embed(title="Music Queue", description=desc, color=0x9B59B6)
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="nowplaying", description="Show what's currently playing", guild=GUILD_ID)
async def nowplaying(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    state = get_music_state(interaction.guild.id)
    if not state.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    embed = discord.Embed(title="Now Playing", description=f"[{state.current.title}]({state.current.webpage_url})", color=0x1DB954)
    embed.add_field(name="Duration", value=state.current.format_duration())
    embed.add_field(name="Requested by", value=state.current.requester.mention)
    embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%")
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="volume", description="Set the playback volume (0-100)", guild=GUILD_ID)
async def volume(interaction: discord.Interaction, level: int):
    if not await _require_music_channel(interaction):
        return
    if not 0 <= level <= 100:
        await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    state.volume = level / 100
    await interaction.response.send_message(f"Volume set to {level}% (applies immediately to next track).")

@client.tree.command(name="announce", description="Send an announcement to a specific channel", guild=GUILD_ID)
@app_commands.default_permissions(manage_messages=True)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You don't have permission to make announcements.", ephemeral=True)
        return
    await channel.send(message)
    await interaction.response.send_message(f"Announcement sent to {channel.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "announce", f"Channel: {channel.mention}")

# ── Gaming Community Commands ─────────────────────────────────────────────────

@client.tree.command(name="lfg", description="Post a Looking For Group message", guild=GUILD_ID)
async def lfg(interaction: discord.Interaction, game: str, players_needed: int, description: str = ""):
    channel = discord.utils.get(interaction.guild.text_channels, name=LFG_CHANNEL_NAME)
    if not channel:
        try:
            channel = await interaction.guild.create_text_channel(LFG_CHANNEL_NAME, reason="LFG channel")
        except Exception:
            await interaction.response.send_message("Could not find or create a looking-for-group channel.", ephemeral=True)
            return
    embed = discord.Embed(title=f"🎮 LFG — {game}", color=0x00BFFF)
    embed.add_field(name="Player", value=interaction.user.mention, inline=True)
    embed.add_field(name="Players Needed", value=str(players_needed), inline=True)
    if description:
        embed.add_field(name="Details", value=description, inline=False)
    embed.set_footer(text="React or DM to join!")
    msg = await channel.send(embed=embed)
    await msg.add_reaction("✅")
    await interaction.response.send_message(f"LFG post created in {channel.mention}!", ephemeral=True)

@client.tree.command(name="gamertag", description="Set your gamertag for a platform", guild=GUILD_ID)
async def gamertag_set(interaction: discord.Interaction, platform: str, tag: str):
    platforms = ["PC", "Xbox", "PlayStation", "Nintendo", "Steam", "Epic"]
    platform_clean = platform.strip()
    GAMERTAGS.setdefault(interaction.user.id, {})[platform_clean] = tag
    embed = discord.Embed(title="🎮 Gamertag Saved", color=0x00FF7F)
    embed.add_field(name="Platform", value=platform_clean, inline=True)
    embed.add_field(name="Tag", value=tag, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="gamertags", description="View a player's gamertags", guild=GUILD_ID)
async def gamertags_view(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    tags = GAMERTAGS.get(target.id, {})
    if not tags:
        await interaction.response.send_message(f"{target.display_name} hasn't set any gamertags yet.", ephemeral=True)
        return
    embed = discord.Embed(title=f"🎮 {target.display_name}'s Gamertags", color=0x9B59B6)
    embed.set_thumbnail(url=target.display_avatar.url)
    for platform, tag in tags.items():
        embed.add_field(name=platform, value=tag, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="personalspace", description="Set up the Personal Space system — creates a lobby VC that spawns private rooms (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(category="Category to create the lobby in (optional — uses default if omitted)")
async def personalspace(interaction: discord.Interaction, category: str = ""):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    gid   = guild.id

    # Resolve category
    target_cat = None
    if category:
        target_cat = discord.utils.get(guild.categories, name=category)

    # Check if a lobby already exists for this guild
    existing_lobby_id = PERSONAL_SPACE_LOBBY.get(gid)
    existing_ch = guild.get_channel(existing_lobby_id) if existing_lobby_id else None
    if existing_ch:
        await interaction.followup.send(
            f"✅ Personal Space lobby already exists: {existing_ch.mention}\n"
            f"Members join it to get their own private voice room. Delete it and re-run this command to reset.",
            ephemeral=True,
        )
        return

    try:
        lobby = await guild.create_voice_channel(
            name="➕  Join to Create",
            category=target_cat,
            reason="Personal Space lobby created by /personalspace",
        )
        PERSONAL_SPACE_LOBBY[gid] = lobby.id
        embed = discord.Embed(
            title="🔒 Personal Space System Active!",
            description=(
                f"**Lobby channel created:** {lobby.mention}\n\n"
                "**How it works:**\n"
                "1️⃣ Join **➕ Join to Create** to get your own private voice room\n"
                "2️⃣ Your room is named after you and only you can see it by default\n"
                "3️⃣ Invite friends by right-clicking the channel → Edit → Permissions\n"
                "4️⃣ Room auto-deletes when everyone leaves\n\n"
                "You can rename, set limits, and manage permissions of your own room."
            ),
            color=0x7289DA,
        )
        embed.set_footer(text="Personal Space • Powered by GamingZoneBot")
        await interaction.followup.send(embed=embed, ephemeral=True)
        # Also post a public info message in the current text channel
        if interaction.channel:
            pub = discord.Embed(
                title="🔒 Personal Space Voice Rooms",
                description=(
                    f"Join {lobby.mention} to instantly get your own **private voice channel**!\n"
                    "It disappears automatically when you leave. 🎮"
                ),
                color=0x7289DA,
            )
            await interaction.channel.send(embed=pub)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create lobby: {e}", ephemeral=True)

@client.tree.command(name="setupgames", description="Create game roles, channels, and the game selector (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def setupgames(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to set up game channels.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    everyone = guild.default_role

    # Get or create the Games category
    category = discord.utils.get(guild.categories, name="Games")
    if not category:
        category = await guild.create_category(
            "Games",
            overwrites={everyone: discord.PermissionOverwrite(view_channel=False)},
            reason="Game channel setup",
        )

    created_roles, created_channels = [], []

    for game, _emoji in GAME_LIST:
        role_name = f"Game: {game}"

        # Create role if missing
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, reason="Game channel access role")
            created_roles.append(game)

        game_overwrites = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True),
        }

        # Create text channel if missing
        safe_name = game.lower().replace(" ", "-")
        text_ch = discord.utils.get(category.text_channels, name=safe_name)
        if not text_ch:
            await guild.create_text_channel(safe_name, category=category, overwrites=game_overwrites, reason="Game channel setup")
            created_channels.append(f"#{safe_name}")

        # Create voice channel if missing
        voice_ch = discord.utils.get(category.voice_channels, name=game)
        if not voice_ch:
            await guild.create_voice_channel(game, category=category, overwrites=game_overwrites, reason="Game channel setup")
            created_channels.append(f"🔊 {game}")

    # Post the role buttons in #general-chat
    general_ch = discord.utils.get(guild.text_channels, name="general-chat")
    if not general_ch:
        general_ch = discord.utils.get(guild.text_channels, name="general")

    if general_ch:
        embed = discord.Embed(
            title="🎮 Pick Your Games!",
            description=(
                "Click a button below to get access to that game's channels.\n"
                "Click it again to remove your access."
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name="Available Games",
            value="\n".join(f"{e} **{g}**" for g, e in GAME_LIST),
            inline=False
        )
        embed.set_footer(text="Only you can see the confirmation message.")
        await general_ch.send(embed=embed, view=GameRoleView())

    lines = [f"✅ Setup complete! Role buttons posted in {general_ch.mention if general_ch else '#general-chat'}. "]
    if created_roles:
        lines.append(f"**Roles created:** {', '.join(created_roles)}")
    if created_channels:
        lines.append(f"**Channels created:** {', '.join(created_channels)}")
    await interaction.followup.send("\n".join(lines))
    await _log_admin_cmd(interaction, "setupgames",
                         f"Roles: {', '.join(created_roles) or 'none new'} | "
                         f"Channels: {', '.join(created_channels) or 'none new'}")

# ── Level / XP Commands ───────────────────────────────────────────────────────
@client.tree.command(name="rank", description="Check your text level and XP", guild=GUILD_ID)
async def rank(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    gid = interaction.guild.id
    xp = XP_DATA.get(gid, {}).get(target.id, 0)
    level = _xp_to_level(xp)
    xp_for_next = _xp_required(level + 1)
    # calculate XP within current level
    xp_in_level = xp
    for lvl in range(1, level + 1):
        xp_in_level -= _xp_required(lvl)
    embed = discord.Embed(title=f"📊 {target.display_name}'s Rank", color=0x5865F2)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Total XP", value=str(xp), inline=True)
    embed.add_field(name="Progress", value=f"{max(xp_in_level,0)}/{xp_for_next} XP to level {level+1}", inline=False)
    voice_mins = VOICE_MINUTES.get(gid, {}).get(target.id, 0)
    embed.add_field(name="🎙️ Voice Time", value=f"{voice_mins} min", inline=True)
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="leaderboard", description="Show the top 10 most active members", guild=GUILD_ID)
async def leaderboard(interaction: discord.Interaction):
    gid = interaction.guild.id
    guild_xp = XP_DATA.get(gid, {})
    if not guild_xp:
        await interaction.response.send_message("No XP data yet!", ephemeral=True)
        return
    top = sorted(guild_xp.items(), key=lambda x: x[1], reverse=True)[:10]
    embed = discord.Embed(title="🏆 XP Leaderboard", color=0xF1C40F)
    lines = []
    medals = ["🥇","🥈","🥉"]
    for i, (uid, xp) in enumerate(top):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        lvl  = _xp_to_level(xp)
        prefix = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{prefix} **{name}** — Level {lvl} ({xp} XP)")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

# ── Invite Tracking Commands ──────────────────────────────────────────────────
@client.tree.command(name="invites", description="Check how many members you or someone else has invited", guild=GUILD_ID)
async def invites(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    count = INVITE_COUNTS.get(interaction.guild.id, {}).get(target.id, 0)
    await interaction.response.send_message(
        f"📨 **{target.display_name}** has invited **{count}** member(s) to the server."
    )

# ── Ticket Commands ───────────────────────────────────────────────────────────
@client.tree.command(name="setupticketchannel", description="Post the ticket panel in a channel (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def setupticketchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    embed = discord.Embed(
        title="🎫 Support Tickets",
        description="Need help? Click the button below to open a private support ticket.\nA staff member will assist you as soon as possible.",
        color=0x5865F2,
    )
    embed.set_footer(text="One ticket per member at a time.")
    await channel.send(embed=embed, view=OpenTicketView())
    await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "setupticketchannel", f"Panel posted in {channel.mention}")

# ── Reaction Role Commands ────────────────────────────────────────────────────
@client.tree.command(name="reactionrole", description="Add a reaction role to a message (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def reactionrole(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    try:
        mid = int(message_id)
        msg = await interaction.channel.fetch_message(mid)
    except Exception:
        await interaction.response.send_message("Could not find that message in this channel.", ephemeral=True)
        return
    REACTION_ROLES.setdefault(mid, {})[emoji] = role.id
    await msg.add_reaction(emoji)
    await interaction.response.send_message(
        f"✅ Reaction role set: {emoji} → {role.mention} on message `{mid}`.", ephemeral=True
    )
    await _log_admin_cmd(interaction, "reactionrole", f"{emoji} → {role.mention} on message `{mid}`")

# ── Giveaway Commands ─────────────────────────────────────────────────────────
@client.tree.command(name="giveaway", description="Start a giveaway (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def giveaway(interaction: discord.Interaction, prize: str, duration_minutes: int, winners: int = 1):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    end_time = datetime.datetime.now(datetime.timezone.utc).timestamp() + duration_minutes * 60
    embed = discord.Embed(
        title="🎉 GIVEAWAY!",
        description=(
            f"**Prize:** {prize}\n"
            f"**Winners:** {winners}\n"
            f"**Ends:** <t:{int(end_time)}:R>\n\n"
            f"React with 🎉 to enter!\n"
            f"Hosted by {interaction.user.mention}"
        ),
        color=0xF1C40F,
    )
    embed.set_footer(text="Good luck!")
    await interaction.response.send_message("Giveaway started!", ephemeral=True)
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction("🎉")
    await _log_admin_cmd(interaction, "giveaway", f"Prize: **{prize}** | Winners: {winners} | Duration: {duration_minutes}m")
    GIVEAWAYS[msg.id] = {
        "channel_id": interaction.channel.id,
        "end_time":   end_time,
        "prize":      prize,
        "winners":    winners,
        "host_id":    interaction.user.id,
        "ended":      False,
    }

@client.tree.command(name="endgiveaway", description="End a giveaway early (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def endgiveaway(interaction: discord.Interaction, message_id: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("Invalid message ID.", ephemeral=True)
        return
    if mid not in GIVEAWAYS:
        await interaction.response.send_message("No giveaway found with that message ID.", ephemeral=True)
        return
    GIVEAWAYS[mid]["end_time"] = 0  # trigger on next loop
    await interaction.response.send_message("Giveaway will end on the next check cycle (~30 seconds).", ephemeral=True)
    await _log_admin_cmd(interaction, "endgiveaway", f"Message ID: `{mid}`")

# ── Streamer Alert Commands ───────────────────────────────────────────────────
@client.tree.command(name="addstreamer", description="Add a streamer to follow (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@discord.app_commands.choices(platform=[
    discord.app_commands.Choice(name="Twitch",  value="twitch"),
    discord.app_commands.Choice(name="YouTube", value="youtube"),
])
async def addstreamer(interaction: discord.Interaction, username: str, platform: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    # Check for duplicates
    for s in STREAMERS:
        if s["name"].lower() == username.lower() and s["platform"] == platform:
            await interaction.response.send_message(f"**{username}** on {platform} is already being followed.", ephemeral=True)
            return
    STREAMERS.append({"name": username, "platform": platform, "last_live": False})
    await interaction.response.send_message(
        f"✅ Now following **{username}** on **{platform.title()}**. Alerts go to #{STREAMER_CHANNEL_NAME}.",
        ephemeral=True,
    )
    await _log_admin_cmd(interaction, "addstreamer", f"{username} ({platform.title()})")

@client.tree.command(name="removestreamer", description="Stop following a streamer (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def removestreamer(interaction: discord.Interaction, username: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    before = len(STREAMERS)
    STREAMERS[:] = [s for s in STREAMERS if s["name"].lower() != username.lower()]
    if len(STREAMERS) < before:
        await interaction.response.send_message(f"✅ Removed **{username}** from streamer alerts.", ephemeral=True)
        await _log_admin_cmd(interaction, "removestreamer", f"Removed: {username}")
    else:
        await interaction.response.send_message(f"No streamer named **{username}** found.", ephemeral=True)

@client.tree.command(name="streamers", description="List all followed streamers", guild=GUILD_ID)
async def liststreamers(interaction: discord.Interaction):
    if not STREAMERS:
        await interaction.response.send_message("No streamers are being followed yet. Use `/addstreamer`.", ephemeral=True)
        return
    embed = discord.Embed(title="📡 Followed Streamers", color=0x9146FF)
    lines = [f"{'🔴' if s['last_live'] else '⚫'} **{s['name']}** ({s['platform'].title()})" for s in STREAMERS]
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="bot", description="About this bot and what it can do", guild=GUILD_ID)
async def bot_info(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 About This Bot",
        description=(
            "A fully-featured gaming community bot built for this server. "
            "Here's everything it does:"
        ),
        color=0x5865F2,
    )
    embed.set_thumbnail(url=interaction.guild.me.display_avatar.url)

    embed.add_field(name="🎵 Music Player", value=(
        "Play YouTube audio directly in voice channels.\n"
        "Search with autocomplete, queue songs, control volume, skip, pause & more.\n"
        "Use commands in **#music-channel**."
    ), inline=False)

    embed.add_field(name="🎮 Gaming Tools", value=(
        "Post LFG ads, save gamertags, and unlock hidden game channels via button roles.\n"
        "Use `/setupgames` to create channels for all 15 games."
    ), inline=False)

    embed.add_field(name="📊 Levels & XP", value=(
        "Members earn XP every minute of chatting or being in voice.\n"
        "Level ups are announced in-channel. Use `/rank` and `/leaderboard`."
    ), inline=False)

    embed.add_field(name="🎉 Giveaways", value=(
        "Admins run timed giveaways with `/giveaway`. Members enter by reacting 🎉.\n"
        "Winners are picked randomly when the timer ends."
    ), inline=False)

    embed.add_field(name="🎫 Support Tickets", value=(
        "Members open private support channels via a button panel.\n"
        "Transcripts are saved to #ticket-logs when closed."
    ), inline=False)

    embed.add_field(name="📡 Streamer Alerts", value=(
        "Follow Twitch streamers and get notified in **#streamer-alerts** when they go live.\n"
        "Manage with `/addstreamer`, `/removestreamer`, `/streamers`."
    ), inline=False)

    embed.add_field(name="🏷️ Reaction Roles", value=(
        "Admins add reaction roles to any message with `/reactionrole`.\n"
        "Members react to assign/remove roles automatically."
    ), inline=False)

    embed.add_field(name="📨 Invite Tracking", value=(
        "The bot tracks who invited each member.\n"
        "Welcome messages show the inviter. Use `/invites` to check your count."
    ), inline=False)

    embed.add_field(name="🔔 Social Alerts & Welcome", value=(
        "Rich welcome cards in **#welcome** showing avatar and invite source.\n"
        "Join/leave/boost alerts in **#social-alerts**."
    ), inline=False)

    embed.add_field(name="🎮 Free Games", value=(
        "Automatically posts 100% off Steam games to **#free-games** every 4 hours.\n"
        "Use `/setupfreegames` to create the channel and post current deals immediately."
    ), inline=False)

    embed.add_field(name="🛡️ Auto-Moderation & Logs", value=(
        "Banned word filter + spam timeout. Logs message edits, deletes, role changes.\n"
        "All mod actions logged to the mod log channel."
    ), inline=False)

    embed.set_footer(text="Type /help to see every available command.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="help", description="Show all available bot commands", guild=GUILD_ID)
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Bot Commands",
        description="Here's a full list of available slash commands:",
        color=0x5865F2
    )

    embed.add_field(name="🎵 Music (use in #music-channel)", value=(
        "`/play <query>` — Search & play a song (autocomplete)\n"
        "`/pause` `/resume` `/skip` `/stop` `/leave`\n"
        "`/queue` — View the song queue\n"
        "`/nowplaying` — Current song info\n"
        "`/volume <0-100>` — Set playback volume"
    ), inline=False)

    embed.add_field(name="📊 Levels", value=(
        "`/rank [user]` — Show level, XP & voice time\n"
        "`/leaderboard` — Top 10 XP members"
    ), inline=False)

    embed.add_field(name="🎉 Giveaways *(Admin)*", value=(
        "`/giveaway <prize> <minutes> [winners]` — Start a giveaway\n"
        "`/endgiveaway <message_id>` — End early"
    ), inline=False)

    embed.add_field(name="🎫 Tickets *(Admin setup)*", value=(
        "`/setupticketchannel <channel>` — Post the ticket panel\n"
        "Members click **Open a Ticket** to create a private channel"
    ), inline=False)

    embed.add_field(name="🏷️ Reaction Roles *(Admin)*", value=(
        "`/reactionrole <message_id> <emoji> <role>` — Bind a role to a reaction"
    ), inline=False)

    embed.add_field(name="📡 Streamers *(Admin)*", value=(
        "`/addstreamer <username> <platform>` — Follow a streamer\n"
        "`/removestreamer <username>` — Unfollow\n"
        "`/streamers` — List all followed streamers"
    ), inline=False)

    embed.add_field(name="📨 Invites", value=(
        "`/invites [user]` — Check invite count"
    ), inline=False)

    embed.add_field(name="🎮 Gaming", value=(
        "`/lfg <game> <players> [desc]` — Post a LFG ad\n"
        "`/gamertag <platform> <tag>` — Save gamertag\n"
        "`/gamertags [user]` — View gamertags\n"
        "`/setupgames` — Create game channels & role buttons *(Admin)*"
    ), inline=False)

    embed.add_field(name="🆓 Free Games", value=(
        "`/setupfreegames` — Create #free-games channel and post current Steam deals *(Admin)*"
    ), inline=False)

    embed.add_field(name="⚒️ Moderation", value=(
        "`/ban` `/kick` `/mute` `/unmute` `/timeout` `/clear` `/announce`\n"
        "`/unban <user_id>` — Unban a user by ID\n"
        "`/banlist` — View all currently banned users\n"
        "`/whitelist` `/unwhitelist` — Protect members from mod actions"
    ), inline=False)

    embed.set_footer(text="All commands are slash commands — type / to get started!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="setupfreegames", description="Create #free-games and post current Steam deals now (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def setupfreegames(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    ch = discord.utils.get(guild.text_channels, name=FREE_GAMES_CHANNEL_NAME)
    if not ch:
        ch = await guild.create_text_channel(
            FREE_GAMES_CHANNEL_NAME,
            topic="🎮 Free games on Steam — auto-updated every 4 hours",
            reason="Free games setup",
        )
        intro = discord.Embed(
            title="🎮 Free Games on Steam",
            description=(
                "This channel is automatically updated every **4 hours** "
                "with games that are currently **free to claim** on Steam.\n\n"
                "Each post includes the game image, description, expiry info, "
                "and a **Claim on Steam** button — just click and grab it!"
            ),
            color=0x00C851,
        )
        intro.set_footer(text="Powered by GamerPower + Steam Store API")
        await ch.send(embed=intro)

    await interaction.followup.send(f"✅ Fetching current free games and posting to {ch.mention}…", ephemeral=True)

    loop = asyncio.get_running_loop()
    games = await loop.run_in_executor(None, _fetch_steam_free_games)

    for game in games:
        POSTED_FREE_GAMES.add(game["id"])
    _save_posted_games()
    await _post_free_games(ch, games)

    await interaction.followup.send(
        f"{'✅ Posted **' + str(len(games)) + '** free game(s) with images and claim buttons.' if games else '⚠️ No free games found right now.'} The channel auto-updates every 4 hours.",
        ephemeral=True,
    )
    await _log_admin_cmd(interaction, "setupfreegames", f"Channel: {ch.mention} | Games posted: {len(games)}")


@client.tree.command(name="setupchannels", description="Create the casino, Pokémon battle, and music channels (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def setupchannels(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    await _ensure_feature_channels(guild)
    await _post_verify_embed(guild)
    all_chs = [
        discord.utils.get(guild.text_channels, name=n)
        for n in (GAMBLING_CHANNEL_NAME, POKEMON_CHANNEL_NAME, MUSIC_CHANNEL_NAME)
    ]
    mentions = ", ".join(ch.mention for ch in all_chs if ch)
    await interaction.followup.send(
        f"✅ Channels set up with **@{GAMER_ROLE_NAME}**-only access: {mentions}\n"
        f"**#{VERIFY_CHANNEL_NAME}** created — visible to new members, hidden after they verify.",
        ephemeral=True,
    )
    await _log_admin_cmd(interaction, "setupchannels", f"Channels: {mentions}")


@client.tree.command(name="setupverify", description="Create (or reset) the #✅-verify channel with the verification embed (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def setupverify(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Ensure the Gamer role exists and verify channel has correct perms
    gamer_role = discord.utils.get(guild.roles, name=GAMER_ROLE_NAME)
    if not gamer_role:
        gamer_role = await guild.create_role(
            name=GAMER_ROLE_NAME,
            colour=discord.Colour.green(),
            reason="Auto-created by /setupverify",
        )

    verify_ow = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True
        ),
        gamer_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True
        ),
    }
    for role in guild.roles:
        if role.permissions.administrator:
            verify_ow[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

    verify_ch = discord.utils.get(guild.text_channels, name=VERIFY_CHANNEL_NAME)
    if verify_ch:
        await verify_ch.edit(overwrites=verify_ow, topic="🟢 Click the button to verify and unlock the server!")
        created = False
    else:
        verify_ch = await guild.create_text_channel(
            VERIFY_CHANNEL_NAME,
            overwrites=verify_ow,
            topic="🟢 Click the button to verify and unlock the server!",
        )
        created = True

    # Post the verification embed (clears old bot embeds first so it's always fresh)
    try:
        async for msg in verify_ch.history(limit=50):
            if msg.author == guild.me and msg.embeds:
                await msg.delete()
    except Exception:
        pass

    embed = discord.Embed(
        title="✅  Welcome — Verify to Get Access!",
        description=(
            f"Click the button below to receive the **@{GAMER_ROLE_NAME}** role.\n\n"
            "Once verified you'll unlock:\n"
            f"🎰 **#{GAMBLING_CHANNEL_NAME}** — Casino games\n"
            f"⚔️ **#{POKEMON_CHANNEL_NAME}** — Pokemon battles\n"
            f"🎵 **#{MUSIC_CHANNEL_NAME}** — Music commands\n\n"
            "*This channel will disappear once you verify — out of sight, out of mind!*"
        ),
        color=0x2ECC71,
    )
    embed.set_footer(text="GamingZoneBot • Click once to verify")
    await verify_ch.send(embed=embed, view=GamerVerifyView())

    action = "Created" if created else "Reset"
    await interaction.followup.send(
        f"✅ {action} {verify_ch.mention} — permissions updated and verification embed posted.",
        ephemeral=True,
    )
    await _log_admin_cmd(interaction, "setupverify", f"{action} #{VERIFY_CHANNEL_NAME}")


@client.tree.command(name="createchannel", description="Create a new text channel (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    name="Name of the new channel (no spaces — use dashes)",
    category="Optional: name of an existing category to place it in",
    private="If True, only admins can see it; if False, visible to everyone",
)
async def createchannel(
    interaction: discord.Interaction,
    name: str,
    private: bool = False,
    category: str = "",
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Sanitise name
    channel_name = name.lower().replace(" ", "-")

    # Resolve category if provided
    cat_obj = None
    if category:
        cat_obj = discord.utils.get(guild.categories, name=category)
        if cat_obj is None:
            await interaction.followup.send(f"❌ No category named **{category}** found.", ephemeral=True)
            return

    # Build permission overwrites
    if private:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    else:
        game_overwrites = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        # Admins always see game channels
        for r in guild.roles:
            if r.permissions.administrator:
                game_overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True)

        # Create or update text channel
        safe_name = game.lower().replace(" ", "-")
        text_ch = discord.utils.get(category.text_channels, name=safe_name)
        if not text_ch:
            await guild.create_text_channel(safe_name, category=category, overwrites=game_overwrites, reason="Game channel setup")
            created_channels.append(f"#{safe_name}")
        else:
            await text_ch.edit(overwrites=game_overwrites)

        # Create or update voice channel
        voice_ch = discord.utils.get(category.voice_channels, name=game)
        if not voice_ch:
            await guild.create_voice_channel(game, category=category, overwrites=game_overwrites, reason="Game channel setup")
            created_channels.append(f"🔊 {game}")
        else:
            await voice_ch.edit(overwrites=game_overwrites)
async def movechannel(interaction: discord.Interaction, channel: discord.TextChannel, category: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    cat_obj = discord.utils.find(lambda c: c.name.lower() == category.lower(), guild.categories)
    if cat_obj is None:
        await interaction.followup.send(f"❌ No category named **{category}** found. Check the exact name and try again.", ephemeral=True)
        return
    await channel.edit(category=cat_obj)
    await interaction.followup.send(f"✅ Moved {channel.mention} to category **{cat_obj.name}**.", ephemeral=True)
    await _log_admin_cmd(interaction, "movechannel", f"{channel.mention} → {cat_obj.name}")


# Start web dashboard before bot connects so Railway's health check passes immediately
dashboard.init(
    xp_data=XP_DATA,
    voice_minutes=VOICE_MINUTES,
    invite_counts=INVITE_COUNTS,
    open_tickets=OPEN_TICKETS,
    giveaways=GIVEAWAYS,
    streamers=STREAMERS,
    banned_words=BANNED_WORDS,
    banned_word_warnings=BANNED_WORD_WARNINGS,
    whitelist=WHITELIST,
    music_states=music_states,
    reaction_roles=REACTION_ROLES,
    bot_client=client,
    guild_id=GUILD_ID.id,
    bot_loop=None,  # loop injected after on_ready via update below
    search_youtube_fn=search_youtube,
    play_next_fn=play_next,
    song_entry_cls=SongEntry,
    get_music_state_fn=get_music_state,
)
dashboard.start()

# Start ngrok tunnel so gaming.zone.ngrok.pro routes to the dashboard
_ngrok_authtoken = os.getenv("NGROK_AUTHTOKEN")
_ngrok_domain    = os.getenv("NGROK_DOMAIN", "gaming.zone.ngrok.pro")
if _ngrok_authtoken:
    try:
        from pyngrok import ngrok as _ngrok, conf as _ngrok_conf
        _ngrok_conf.get_default().auth_token = _ngrok_authtoken
        _tunnel = _ngrok.connect(dashboard.DASHBOARD_PORT, "http", hostname=_ngrok_domain, pooling_enabled=True)
        print(f"[ngrok] Tunnel active: {_tunnel.public_url}")
    except Exception as _e:
        print(f"[ngrok] Warning: tunnel failed to start — {_e}")

client.run(os.getenv('BOT_TOKEN'))