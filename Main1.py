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

GAMING_ZONE_GUILD_ID = 711335159189864468
GUILD_ID  = discord.Object(id=GAMING_ZONE_GUILD_ID)
GUILD_ID_2 = discord.Object(id=1495449662755442698)
# Focus bot on Gaming Zone only.
PRIMARY_GUILD_NAME = os.getenv("PRIMARY_GUILD_NAME", "Gaming Zone").strip()
PRIMARY_GUILD_ID = int(os.getenv("PRIMARY_GUILD_ID", str(GAMING_ZONE_GUILD_ID)))
AUTO_LEAVE_NON_PRIMARY_GUILDS = os.getenv("AUTO_LEAVE_NON_PRIMARY_GUILDS", "1").lower() in {"1", "true", "yes", "on"}


class Client(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._feature_cmds_registered = False
        self._startup_completed = False
        self._disconnect_started_at = None

    @staticmethod
    def _format_outage(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        mins, secs = divmod(seconds, 60)
        if mins < 60:
            return f"{mins}m {secs}s"
        hours, mins = divmod(mins, 60)
        return f"{hours}h {mins}m {secs}s"

    async def _send_bot_runtime_event(self, title: str, description: str, color: int) -> None:
        for guild in self.guilds:
            ch = _resolve_or_track_text_channel(guild, "bot_log", BOT_LOG_NAME, "bot-logs")
            if not ch:
                continue
            try:
                embed = discord.Embed(title=title, description=description, color=color)
                embed.timestamp = discord.utils.utcnow()
                await ch.send(embed=embed)
            except Exception as e:
                print(f"[BotLog] Failed runtime event send in {guild.name} ({guild.id}): {e}")

    async def _find_recent_delete_actor(
        self,
        guild: discord.Guild,
        *,
        channel_id: int,
        target_id: int | None = None,
    ) -> tuple[discord.abc.User | discord.Member | None, int | None]:
        try:
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.message_delete):
                age_seconds = (now - entry.created_at).total_seconds()
                if age_seconds > 15:
                    break
                extra = getattr(entry, "extra", None)
                extra_channel = getattr(extra, "channel", None)
                if extra_channel and getattr(extra_channel, "id", None) != channel_id:
                    continue
                target = getattr(entry, "target", None)
                if target_id is not None and getattr(target, "id", None) not in {target_id, None}:
                    continue
                return entry.user, getattr(extra, "count", None)
        except Exception as e:
            print(f"[Audit] Could not resolve delete actor in {guild.name} ({guild.id}): {e}")
        return None, None

    async def _announce_recovered_if_needed(self, source: str) -> None:
        if not self._disconnect_started_at:
            return
        outage_seconds = max(1, int((discord.utils.utcnow() - self._disconnect_started_at).total_seconds()))
        self._disconnect_started_at = None
        await self._send_bot_runtime_event(
            "🟢 Bot Back Online",
            (
                f"Gateway connection restored via **{source}**.\n"
                f"Estimated outage: **{self._format_outage(outage_seconds)}**."
            ),
            0x2ECC71,
        )

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
                    f"Check out the rules and enjoy your stay!\n"
                    f"Then head to **#{VERIFY_CHANNEL_NAME}** and click **Verify — Get Access** to unlock channels."
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

        # ── Social alert in the configured social alerts channel ──────────
        alert_ch = _resolve_social_alert_channel(member.guild)
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
        alert_ch = _resolve_social_alert_channel(member.guild)
        if alert_ch:
            embed = discord.Embed(
                description=f"📤 **{member}** left the server.",
                color=0xE74C3C,
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            try:
                await alert_ch.send(embed=embed)
            except discord.Forbidden:
                print(f"[Social] Missing access to send leave alert in {member.guild.name} ({member.guild.id})")
            except Exception as e:
                print(f"[Social] Leave alert failed in {member.guild.name} ({member.guild.id}): {e}")

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # ── Social alert when someone boosts ─────────────────────────────
        if before.premium_since is None and after.premium_since is not None:
            alert_ch = _resolve_social_alert_channel(after.guild)
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
        log_ch = _resolve_mod_log_channel(message.guild)
        if log_ch:
            actor, deleted_count = await self._find_recent_delete_actor(
                message.guild,
                channel_id=message.channel.id,
                target_id=message.author.id,
            )
            embed = discord.Embed(title="🗑️ Message Deleted", color=0xE74C3C)
            embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
            embed.add_field(name="Channel", value=message.channel.mention, inline=False)
            embed.add_field(name="Content", value=message.content[:1024] or "*empty*", inline=False)
            if actor:
                actor_value = f"{actor.mention} (`{actor.id}`)"
                if deleted_count:
                    actor_value += f"\nAudit count: `{deleted_count}`"
                embed.add_field(name="Deleted By", value=actor_value, inline=False)
            embed.timestamp = discord.utils.utcnow()
            await log_ch.send(embed=embed)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Log deletes even when the message was not cached by the gateway."""
        if payload.guild_id is None:
            return
        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return
        log_ch = _resolve_mod_log_channel(guild)
        if log_ch is None:
            return

        channel_mention = f"<#{payload.channel_id}>"
        cached = payload.cached_message
        if cached is not None and cached.author and cached.author.bot:
            return

        if cached is not None and cached.author is not None:
            actor, deleted_count = await self._find_recent_delete_actor(
                guild,
                channel_id=payload.channel_id,
                target_id=cached.author.id,
            )
        else:
            actor, deleted_count = await self._find_recent_delete_actor(
                guild,
                channel_id=payload.channel_id,
            )

        embed = discord.Embed(title="🗑️ Message Deleted", color=0xE74C3C)
        embed.add_field(name="Channel", value=channel_mention, inline=False)
        embed.add_field(name="Message ID", value=f"`{payload.message_id}`", inline=True)
        if cached is not None:
            embed.set_author(name=str(cached.author), icon_url=cached.author.display_avatar.url)
            embed.add_field(name="Content", value=cached.content[:1024] or "*empty*", inline=False)
        else:
            embed.add_field(name="Content", value="*Unavailable (message not cached)*", inline=False)
        if actor:
            actor_value = f"{actor.mention} (`{actor.id}`)"
            if deleted_count:
                actor_value += f"\nAudit count: `{deleted_count}`"
            embed.add_field(name="Deleted By", value=actor_value, inline=False)
        embed.timestamp = discord.utils.utcnow()
        await log_ch.send(embed=embed)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        """Log bulk deletions (purges) with count and message IDs."""
        if payload.guild_id is None:
            return
        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return
        log_ch = _resolve_mod_log_channel(guild)
        if log_ch is None:
            return

        ids = sorted(payload.message_ids)
        sample = ", ".join(f"`{mid}`" for mid in ids[:20])
        extra = "" if len(ids) <= 20 else f"\n...and {len(ids) - 20} more"
        actor, deleted_count = await self._find_recent_delete_actor(
            guild,
            channel_id=payload.channel_id,
        )

        embed = discord.Embed(title="🧹 Bulk Messages Deleted", color=0xE67E22)
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=False)
        embed.add_field(name="Count", value=str(len(ids)), inline=True)
        embed.add_field(name="Message IDs", value=(sample + extra) if sample else "*Unavailable*", inline=False)
        if actor:
            actor_value = f"{actor.mention} (`{actor.id}`)"
            if deleted_count:
                actor_value += f"\nAudit count: `{deleted_count}`"
            embed.add_field(name="Deleted By", value=actor_value, inline=False)
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
        print(f"[Startup] Marker={STARTUP_MARKER} PID={os.getpid()} Guilds={len(self.guilds)}")
        if self._startup_completed:
            # on_ready can fire multiple times (reconnects); avoid re-running startup setup.
            await self._announce_recovered_if_needed("on_ready")
            return
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
            # Resolve primary guild by name first (Gaming Zone), then ID fallback.
            primary_guild = discord.utils.find(
                lambda g: g.name.casefold() == PRIMARY_GUILD_NAME.casefold(),
                self.guilds,
            )
            if primary_guild is None:
                primary_guild = self.get_guild(PRIMARY_GUILD_ID)
            if primary_guild is None:
                print(f"[Startup] WARNING: could not resolve primary guild by name '{PRIMARY_GUILD_NAME}' or ID {PRIMARY_GUILD_ID}.")
            else:
                print(f"[Startup] Primary guild locked to {primary_guild.name} ({primary_guild.id})")

            # Optionally leave all non-primary guilds so activity/logs stay in Gaming Zone only.
            if AUTO_LEAVE_NON_PRIMARY_GUILDS and primary_guild is not None:
                for g in list(self.guilds):
                    if g.id == primary_guild.id:
                        continue
                    try:
                        print(f"[Startup] Leaving non-primary guild {g.name} ({g.id})")
                        await g.leave()
                    except Exception as e:
                        print(f"[Startup] Could not leave guild {g.name} ({g.id}): {e}")

            if primary_guild is not None:
                try:
                    invites = await primary_guild.invites()
                    INVITE_CACHE[primary_guild.id] = {inv.code: inv for inv in invites}
                except Exception:
                    pass
                try:
                    await _ensure_social_alert_channel(primary_guild)
                except Exception as e:
                    print(f"[Social] Startup social channel check failed in {primary_guild.name}: {e}")
                # Keep managed channel mappings scoped to the active primary guild only.
                keep_gid = str(primary_guild.id)
                stale_gids = [gid for gid in MANAGED_CHANNEL_IDS.keys() if gid != keep_gid]
                for gid in stale_gids:
                    del MANAGED_CHANNEL_IDS[gid]
                if stale_gids:
                    _save_managed_channels()
            # Start background tasks once
            if not giveaway_check.is_running():
                giveaway_check.start()
            if not streamer_check.is_running():
                streamer_check.start()
            if not free_games_check.is_running():
                free_games_check.start()
            if not empty_vc_cleanup.is_running():
                empty_vc_cleanup.start()
            if not ticket_sla_check.is_running():
                ticket_sla_check.start()

            # Register grouped feature commands once (global)
            if not self._feature_cmds_registered:
                pokemon_game.setup_pokemon(self)
                pokemon_game.setup_pokemon_economy(self)
                gambling.setup_gambling(self)
                self._feature_cmds_registered = True

            # Do NOT auto-create channels on startup/restart.
            # Only validate and warn in the primary guild; admins can run setup commands explicitly.
            marker_guild = primary_guild
            marker_ch = None
            role_ch = None
            ticket_ch = None
            if marker_guild is not None:
                # Always reconcile log channels in the primary guild (moves/renames to target names/category).
                try:
                    await _ensure_log_channels(marker_guild)
                except Exception as e:
                    print(f"[Startup] Could not ensure log channels in {marker_guild.name} ({marker_guild.id}): {e}")
                marker_ch = _resolve_or_track_text_channel(marker_guild, "bot_log", BOT_LOG_NAME, "bot-logs")
                role_ch = _resolve_or_track_text_channel(marker_guild, "role_log", ROLE_LOG_NAME, "role-logs")
                ticket_ch = _resolve_ticket_log_channel(marker_guild)

                if marker_ch:
                    print(f"[Startup] Log route bot: #{marker_ch.name} ({marker_ch.id})")
                if role_ch:
                    print(f"[Startup] Log route role: #{role_ch.name} ({role_ch.id})")
                if ticket_ch:
                    print(f"[Startup] Log route ticket: #{ticket_ch.name} ({ticket_ch.id})")

            if marker_guild is None:
                print("[Startup] ⚠️  No primary guild resolved; startup marker not sent.")
            elif marker_ch is None:
                print(
                    f"[Startup] ⚠️  No bot-logs channel found in primary guild "
                    f"{marker_guild.name} ({marker_guild.id}); startup marker not sent."
                )
            else:
                try:
                    marker_embed = discord.Embed(title="🟢 Bot Startup Marker", color=0x2ECC71)
                    marker_embed.add_field(name="Marker", value=f"`{STARTUP_MARKER}`", inline=False)
                    marker_embed.add_field(name="PID", value=f"`{os.getpid()}`", inline=True)
                    marker_embed.add_field(name="Guild", value=f"`{marker_guild.id}`", inline=True)
                    marker_embed.timestamp = discord.utils.utcnow()
                    await marker_ch.send(embed=marker_embed)
                    print(f"[Startup] ✅ Sent startup marker to {marker_ch.mention} in {marker_guild.name} ({marker_guild.id})")
                except Exception as e:
                    print(f"[Startup] Error sending marker: {e}")
                    import traceback
                    traceback.print_exc()

            if primary_guild is not None:
                try:
                    if _resolve_ticket_log_channel(primary_guild) is None:
                        print(f"[Startup] Missing ticket log channel in {primary_guild.name} (expected #{TICKET_LOG_NAME})")
                except Exception as e:
                    print(f"[Startup] Ticket log validation failed in {primary_guild.name}: {e}")

            # Sync slash commands only to the resolved primary guild.
            try:
                if primary_guild is None:
                    print("[Sync] Skipped guild sync because no primary guild was resolved.")
                else:
                    guild_obj = discord.Object(id=primary_guild.id)
                    synced_guild = await self.tree.sync(guild=guild_obj)
                    print(f"Synced {len(synced_guild)} slash commands to primary guild {primary_guild.id}")
                    try:
                        synced_names = ", ".join(sorted(cmd.name for cmd in synced_guild))
                        print(f"[Sync] Commands: {synced_names}")
                    except Exception:
                        pass
            except Exception as e:
                print(f"[Sync] Could not sync resolved primary guild: {e}")
            # Inject the running event loop into the dashboard now that the bot is connected
            dashboard._state["bot_loop"] = asyncio.get_event_loop()
            self._startup_completed = True

            await self._announce_recovered_if_needed("on_ready")
        except Exception as e:
            print(f'Error syncing commands: {e}')

    async def on_disconnect(self):
        # Called when gateway disconnects; Discord will usually auto-reconnect.
        if self._disconnect_started_at is not None:
            return
        self._disconnect_started_at = discord.utils.utcnow()
        await self._send_bot_runtime_event(
            "🔴 Bot Offline Detected",
            "Gateway connection dropped. Attempting automatic reconnect...",
            0xE74C3C,
        )

    async def on_resumed(self):
        await self._announce_recovered_if_needed("on_resumed")

    async def on_guild_join(self, guild: discord.Guild):
        if guild.name.casefold() != PRIMARY_GUILD_NAME.casefold() and guild.id != PRIMARY_GUILD_ID:
            print(f"[Guild Join] Non-primary guild joined: {guild.name} ({guild.id})")
            if AUTO_LEAVE_NON_PRIMARY_GUILDS:
                try:
                    await guild.leave()
                    print(f"[Guild Join] Left non-primary guild {guild.name} ({guild.id})")
                except Exception as e:
                    print(f"[Guild Join] Could not leave non-primary guild {guild.name} ({guild.id}): {e}")
            return
        try:
            synced = await self.tree.sync(guild=guild)
            print(f"[Guild Join] Synced {len(synced)} slash commands to {guild.name} ({guild.id})")
            print("[Guild Join] Auto channel creation is disabled. Use setup commands to create channels.")
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

LOG_CHANNEL_ID = 1496672123551355062
LOGS_CATEGORY_ID = 1496416640022220902
TICKET_LOG_CHANNEL_ID = 1496673347533144094
BOT_LOG_CHANNEL_ID = 1498287670005071884
TICKET_LOG_CATEGORY_ID = LOGS_CATEGORY_ID
TICKET_LOG_NAME = "🎫┃ticket-logs"
SOCIAL_ALERTS_CHANNEL_NAME = "📣┃social-alerts"
LEGACY_SOCIAL_ALERTS_CHANNEL_NAME = "social-alerts"
MOD_LOG_NAME  = "📋┃𝗆𝗈𝖽-𝗅𝗈𝗀𝗌"   # admin-only mod action log
ROLE_LOG_NAME = "📜┃𝗋𝗈𝗅𝖾-𝗅𝗈𝗀𝗌"  # admin-only role assignment log
BOT_LOG_NAME  = "🤖┃bot-logs"   # private bot runtime/economy event log
GAMBLING_CHANNEL_NAME = "casino-floor"   # dedicated casino text channel
POKEMON_CHANNEL_NAME  = "pokemon-battle" # dedicated pokemon battle channel
GAMER_ROLE_NAME       = "Gamer"          # role granted on verification — unlocks feature channels
VERIFY_CHANNEL_NAME   = "✅-verify"       # visible to unverified; hidden once Gamer role is granted
VERIFY_REWARD_COINS   = 1800              # one-time reward when a member verifies
MUSIC_CATEGORY_NAME   = "♦┃𝙏𝙚𝙭𝙩 𝘾𝙝𝙖𝙣𝙣𝙚𝙡𝙨┃♦"  # category where music-channel is created
WHITELIST: set[int] = set()  # stores whitelisted user IDs
VERIFY_EMBED_MARKER = "GZ_VERIFY_EMBED_V1"
_MANAGED_CHANNELS_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "managed_channels.json")
MANAGED_CHANNEL_IDS: dict[str, dict[str, int]] = {}
STARTUP_MARKER = f"boot-{int(time.time())}-{os.getpid()}"


def _load_managed_channels() -> None:
    global MANAGED_CHANNEL_IDS
    if not os.path.exists(_MANAGED_CHANNELS_SAVE):
        return
    try:
        with open(_MANAGED_CHANNELS_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            MANAGED_CHANNEL_IDS = {str(gid): {str(k): int(v) for k, v in mapping.items()} for gid, mapping in data.items() if isinstance(mapping, dict)}
    except Exception as e:
        print(f"[Channels] Warning: could not load managed channel ids — {e}")


def _save_managed_channels() -> None:
    try:
        with open(_MANAGED_CHANNELS_SAVE, "w", encoding="utf-8") as f:
            json.dump(MANAGED_CHANNEL_IDS, f)
    except Exception as e:
        print(f"[Channels] Warning: could not save managed channel ids — {e}")


def _remember_channel(guild: discord.Guild, key: str, channel: discord.abc.GuildChannel) -> None:
    gid = str(guild.id)
    MANAGED_CHANNEL_IDS.setdefault(gid, {})[key] = int(channel.id)
    _save_managed_channels()


def _tracked_text_channel(guild: discord.Guild, key: str) -> discord.TextChannel | None:
    cid = MANAGED_CHANNEL_IDS.get(str(guild.id), {}).get(key)
    if not cid:
        return None
    ch = guild.get_channel(cid)
    return ch if isinstance(ch, discord.TextChannel) else None


def _match_channel_by_key(guild: discord.Guild, key: str) -> discord.TextChannel | None:
    for ch in guild.text_channels:
        topic = (ch.topic or "").casefold()
        if not topic:
            continue
        if key == "casino_channel" and (
            "/slots" in topic or "/blackjack" in topic or "/roulette" in topic or "casino" in topic
        ):
            return ch
        if key == "pokemon_channel" and (
            "/pokemon battle" in topic or "pokemon battles" in topic or "challenge others to pokemon" in topic
        ):
            return ch
        if key == "music_channel" and (
            "request music" in topic or "/play /skip /queue" in topic or "music commands" in topic
        ):
            return ch
        if key == "verify_channel" and "verify" in topic and "unlock" in topic:
            return ch
        if key == "free_games_channel" and ("free game" in topic or "steam deals" in topic):
            return ch
    return None


def _resolve_or_track_text_channel(guild: discord.Guild, key: str, *names: str) -> discord.TextChannel | None:
    tracked = _tracked_text_channel(guild, key)
    if tracked:
        return tracked
    found = _find_text_channel_ci(guild, *names)
    if not found:
        found = _match_channel_by_key(guild, key)
    if found:
        _remember_channel(guild, key, found)
    return found


_load_managed_channels()


def _find_text_channel_ci(guild: discord.Guild, *names: str) -> discord.TextChannel | None:
    """Case-insensitive text channel lookup across one or more possible names."""
    wanted = {n.lower() for n in names if n}
    for ch in guild.text_channels:
        if ch.name.lower() in wanted:
            return ch
    return None


def _resolve_social_alert_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Resolve social alerts channel by preferred emoji name, then legacy name."""
    return _find_text_channel_ci(guild, SOCIAL_ALERTS_CHANNEL_NAME, LEGACY_SOCIAL_ALERTS_CHANNEL_NAME)


async def _ensure_social_alert_channel(guild: discord.Guild) -> None:
    """Rename legacy social alerts channel to the emoji style name when present."""
    ch = _resolve_social_alert_channel(guild)
    if not ch:
        return
    if ch.name != SOCIAL_ALERTS_CHANNEL_NAME:
        try:
            await ch.edit(name=SOCIAL_ALERTS_CHANNEL_NAME)
            print(f"[Social] Renamed #{LEGACY_SOCIAL_ALERTS_CHANNEL_NAME} to #{SOCIAL_ALERTS_CHANNEL_NAME} in {guild.name}")
        except Exception as e:
            print(f"[Social] Could not rename social alerts channel in {guild.name}: {e}")


def _resolve_mod_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Resolve moderation log channel by legacy ID first, then by configured name."""
    ch = guild.get_channel(LOG_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        _remember_channel(guild, "mod_log", ch)
        return ch
    return _resolve_or_track_text_channel(guild, "mod_log", MOD_LOG_NAME, "mod-logs")


def _resolve_ticket_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Resolve ticket log channel with tolerant name matching."""
    configured = guild.get_channel(TICKET_LOG_CHANNEL_ID)
    if isinstance(configured, discord.TextChannel):
        _remember_channel(guild, "ticket_log", configured)
        return configured
    tracked = _tracked_text_channel(guild, "ticket_log")
    if tracked:
        return tracked
    ticket_cat = guild.get_channel(TICKET_LOG_CATEGORY_ID)
    if isinstance(ticket_cat, discord.CategoryChannel):
        wanted = {TICKET_LOG_NAME.lower(), "ticket logs", "ticket-log", "ticket_logs"}
        for ch in ticket_cat.text_channels:
            if ch.name.lower() in wanted:
                _remember_channel(guild, "ticket_log", ch)
                return ch
    return _resolve_or_track_text_channel(guild, "ticket_log", TICKET_LOG_NAME, "ticket logs", "ticket-log", "ticket_logs")


async def _ensure_log_channels(guild: discord.Guild) -> None:
    """Create private moderation, role, and bot logs channels."""
    logs_category = guild.get_channel(LOGS_CATEGORY_ID)
    category_kwargs = {"category": logs_category} if isinstance(logs_category, discord.CategoryChannel) else {}
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
    for key, ch_name, legacy_name, topic in [
        ("mod_log", MOD_LOG_NAME, "mod-logs", "🔨 Private admin log — every mod command is recorded here."),
        ("role_log", ROLE_LOG_NAME, "role-logs", "🏷️ Private role log — all role picks from embeds are recorded here."),
        ("bot_log", BOT_LOG_NAME, "bot-logs", "🤖 Private bot log — runtime and economy automation events."),
    ]:
        wanted_names = {ch_name.casefold(), legacy_name.casefold()}
        tracked = _tracked_text_channel(guild, key)
        candidates = [ch for ch in guild.text_channels if ch.name.casefold() in wanted_names]

        existing = None
        if tracked and tracked.name.casefold() in wanted_names:
            existing = tracked
        elif isinstance(logs_category, discord.CategoryChannel):
            existing = next((ch for ch in candidates if ch.category and ch.category.id == logs_category.id), None)
        if existing is None and candidates:
            existing = candidates[0]

        if not existing:
            existing = await guild.create_text_channel(ch_name, overwrites=admin_ow, topic=topic, **category_kwargs)
            print(f"[Logs] Created #{ch_name} in {guild.name}")
        edit_kwargs = {"overwrites": admin_ow, "topic": topic}
        if existing.name != ch_name:
            edit_kwargs["name"] = ch_name
        if isinstance(logs_category, discord.CategoryChannel) and existing.category != logs_category:
            edit_kwargs["category"] = logs_category
        await existing.edit(**edit_kwargs)
        if existing.name != ch_name:
            print(f"[Logs] Renamed #{legacy_name} to #{ch_name} in {guild.name}")
        _remember_channel(guild, key, existing)

        # Remove duplicate legacy/target channels in the logs category.
        for dup in candidates:
            if dup.id == existing.id:
                continue
            if isinstance(logs_category, discord.CategoryChannel) and dup.category and dup.category.id == logs_category.id:
                try:
                    await dup.delete(reason=f"Cleanup duplicate log channel; keeping {existing.name} ({existing.id})")
                    print(f"[Logs] Deleted duplicate #{dup.name} ({dup.id}) in {guild.name}")
                except Exception as e:
                    print(f"[Logs] Could not delete duplicate #{dup.name} ({dup.id}) in {guild.name}: {e}")
            else:
                print(f"[Logs] Found duplicate #{dup.name} ({dup.id}) outside logs category; left untouched")

    # Ticket logs channel stays separate from mod/role logs and prefers the configured channel ID.
    ticket_topic = "🎫 Private ticket transcript log — closed ticket history is saved here."
    configured_ticket = guild.get_channel(TICKET_LOG_CHANNEL_ID)
    ticket_cat = None
    if isinstance(configured_ticket, discord.TextChannel) and isinstance(configured_ticket.category, discord.CategoryChannel):
        ticket_cat = configured_ticket.category
    else:
        resolved_ticket_cat = guild.get_channel(TICKET_LOG_CATEGORY_ID)
        if isinstance(resolved_ticket_cat, discord.CategoryChannel):
            ticket_cat = resolved_ticket_cat

    existing_ticket_log = configured_ticket if isinstance(configured_ticket, discord.TextChannel) else _resolve_ticket_log_channel(guild)
    if existing_ticket_log:
        edit_kwargs = {"overwrites": admin_ow, "topic": ticket_topic}
        if existing_ticket_log.name != TICKET_LOG_NAME:
            edit_kwargs["name"] = TICKET_LOG_NAME
        if isinstance(ticket_cat, discord.CategoryChannel) and existing_ticket_log.category != ticket_cat:
            edit_kwargs["category"] = ticket_cat
        await existing_ticket_log.edit(**edit_kwargs)
        _remember_channel(guild, "ticket_log", existing_ticket_log)

        # Remove duplicate ticket-log channels in the ticket category.
        ticket_aliases = {TICKET_LOG_NAME.casefold(), "ticket logs", "ticket-log", "ticket_logs"}
        for dup in guild.text_channels:
            if dup.id == existing_ticket_log.id:
                continue
            if dup.name.casefold() not in ticket_aliases:
                continue
            if isinstance(ticket_cat, discord.CategoryChannel) and dup.category and dup.category.id == ticket_cat.id:
                try:
                    await dup.delete(reason=f"Cleanup duplicate ticket log channel; keeping {existing_ticket_log.name} ({existing_ticket_log.id})")
                    print(f"[Logs] Deleted duplicate ticket log #{dup.name} ({dup.id}) in {guild.name}")
                except Exception as e:
                    print(f"[Logs] Could not delete duplicate ticket log #{dup.name} ({dup.id}) in {guild.name}: {e}")
    else:
        create_kwargs = {"overwrites": admin_ow, "topic": ticket_topic}
        if isinstance(ticket_cat, discord.CategoryChannel):
            create_kwargs["category"] = ticket_cat
        created = await guild.create_text_channel(TICKET_LOG_NAME, **create_kwargs)
        _remember_channel(guild, "ticket_log", created)
        print(f"[Logs] Created #{TICKET_LOG_NAME} in {guild.name}")

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
        (GAMBLING_CHANNEL_NAME, "🎰 Use all casino commands here! /slots /blackjack /poker /crash and more", None, ["casino-floor", "casino"]),
        (POKEMON_CHANNEL_NAME,  "⚔️ Challenge others to Pokemon battles here! /pokemon battle", None, ["pokemon-battle", "pokemon"]),
        (MUSIC_CHANNEL_NAME,    "🎵 Request music and use all music commands here! /play /skip /queue", music_category if music_category else None, ["music-channel", "music"]),
    ]
    for idx, (ch_name, topic, category, aliases) in enumerate(channels):
        key = ["casino_channel", "pokemon_channel", "music_channel"][idx]
        ch = _resolve_or_track_text_channel(guild, key, ch_name, *aliases)
        if ch:
            # Update existing channel perms (and move to correct category if set)
            edit_kwargs = {"overwrites": restricted_ow}
            if category and ch.category != category:
                edit_kwargs["category"] = category
            await ch.edit(**edit_kwargs)
            _remember_channel(guild, key, ch)
        else:
            ch = await guild.create_text_channel(ch_name, overwrites=restricted_ow, topic=topic, category=category)
            _remember_channel(guild, key, ch)
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
    verify_ch = _resolve_or_track_text_channel(guild, "verify_channel", VERIFY_CHANNEL_NAME, "verify", "✅-verify", "-verify")
    if verify_ch is None:
        # Last-resort fallback: reuse any prior verify-like channel by topic marker.
        verify_ch = next(
            (
                c for c in guild.text_channels
                if c.topic and "verify" in c.topic.lower() and "unlock" in c.topic.lower()
            ),
            None,
        )
        if verify_ch:
            _remember_channel(guild, "verify_channel", verify_ch)
    if verify_ch:
        await verify_ch.edit(overwrites=verify_ow)
        _remember_channel(guild, "verify_channel", verify_ch)
    else:
        verify_ch = await guild.create_text_channel(
            VERIFY_CHANNEL_NAME,
            overwrites=verify_ow,
            topic="🟢 Click the button to verify and unlock the server!",
        )
        _remember_channel(guild, "verify_channel", verify_ch)
        print(f"[Channels] Created #{VERIFY_CHANNEL_NAME} in {guild.name}")

async def _post_verify_embed(guild: discord.Guild) -> None:
    """Post the verification embed in #verify (visible to new members, hidden after they verify)."""
    verify_ch = _find_text_channel_ci(guild, VERIFY_CHANNEL_NAME, "verify", "-verify")
    if not verify_ch:
        return

    # Don't repost if there's already a verify message (check pins first, then recent history).
    try:
        for msg in await verify_ch.pins():
            if msg.author == guild.me and msg.embeds and msg.embeds[0].footer and msg.embeds[0].footer.text and VERIFY_EMBED_MARKER in msg.embeds[0].footer.text:
                return
        async for msg in verify_ch.history(limit=150):
            if msg.author == guild.me and msg.embeds and msg.embeds[0].footer and msg.embeds[0].footer.text and VERIFY_EMBED_MARKER in msg.embeds[0].footer.text:
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
    embed.set_footer(text=f"GamingZoneBot • Click once to verify • {VERIFY_EMBED_MARKER}")
    try:
        verify_msg = await verify_ch.send(embed=embed, view=GamerVerifyView())
        try:
            await verify_msg.pin(reason="Keep a single canonical verification message across restarts")
        except Exception:
            pass
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


def _format_uptime(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)

# ── Voice Time Tracking ───────────────────────────────────────────────────────
VOICE_JOIN_TIME: dict[int, dict[int, float]] = {}  # guild_id -> {user_id -> join_timestamp}
VOICE_MINUTES:   dict[int, dict[int, int]]   = {}  # guild_id -> {user_id -> total_minutes}

# ── Invite Tracking ───────────────────────────────────────────────────────────
INVITE_CACHE:  dict[int, dict[str, discord.Invite]] = {}  # guild_id -> {code -> Invite}
INVITE_COUNTS: dict[int, dict[int, int]]            = {}  # guild_id -> {inviter_id -> count}

# ── Tickets ───────────────────────────────────────────────────────────────────
OPEN_TICKETS: dict[int, int] = {}  # user_id -> channel_id
TICKET_CATEGORY_NAME = "Support Tickets"
TICKET_LOG_NAME      = "🎫┃ticket-logs"
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
FREE_GAMES_FIRST_CYCLE = True

# ── LFG + RSVP Tracking ─────────────────────────────────────────────────────
LFG_POSTS: dict[int, dict] = {}  # message_id -> metadata
LFG_RSVP: dict[int, dict[str, set[int]]] = {}  # message_id -> {join|maybe|pass -> set(user_ids)}

# ── Prestige System ────────────────────────────────────────────────────────
# Enables players to "reset" and gain permanent bonuses at higher levels
_PRESTIGE_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prestige_data.json")
PRESTIGE_DATA: dict[int, dict[str, int]] = {}  # user_id -> {prestige: int, resets: int, total_xp_earned: int}
PRESTIGE_LEVELS = 10  # max prestige level
PRESTIGE_BONUS_XP_PER_LEVEL = 0.05  # 5% XP bonus per prestige level
PRESTIGE_RESET_COST = 2500  # PokeCoins to reset and gain prestige (from gambling.py)

# ── Economy Sinks + Cosmetics ────────────────────────────────────────────────
COSMETICS = {
    "title_badge": {"name": "Title Badge", "cost": 500, "desc": "Custom title prefix in chat"},
    "border_glow": {"name": "Border Glow", "cost": 300, "desc": "Glowing effect on battle embeds"},
    "avatar_frame": {"name": "Avatar Frame", "cost": 400, "desc": "Custom frame around your profile"},
    "speed_boost": {"name": "Speed Boost", "cost": 1000, "desc": "+10% to all XP gains for 1 week"},
}
_COSMETICS_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cosmetics_inventory.json")
PLAYER_COSMETICS: dict[int, set[str]] = {}  # user_id -> {cosmetic_ids}

# ── Ticket SLA Monitoring ───────────────────────────────────────────────────
TICKET_SLA_HOURS = 6
TICKET_SLA_REMINDER_COOLDOWN_MINUTES = 180
TICKET_SLA_LAST_REMINDER: dict[int, float] = {}  # channel_id -> unix timestamp

# ── Phase 4: Moderation Safety Automation ──────────────────────────────────
# Raid mode: lock down channels during raids or high spam
RAID_MODE_ACTIVE: dict[int, bool] = {}  # guild_id -> is_raid_mode_on
RAID_MODE_ROLES_MUTED: dict[int, set[int]] = {}  # guild_id -> {role_ids that can't message}

# Link quarantine: prevent suspicious URLs (non-Discord, malware-like)
_QUARANTINE_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "link_quarantine.json")
LINK_QUARANTINE: dict[int, dict[str, int]] = {}  # user_id -> {quarantine_level: 0-2, last_violation_time: unix}
LINK_QUARANTINE_LEVELS = 3  # 0=normal, 1=quarantine (needs review), 2=banned

# Account age gate: restrict new accounts from certain features
ACCOUNT_AGE_GATE_DAYS = 7  # minimum account age to post links/participate in gyms/raids
ACCOUNT_AGE_GATE_ENABLED: dict[int, bool] = {}  # guild_id -> is_enabled

# Runtime Metadata
BOT_BOOT_TIME_UTC = discord.utils.utcnow()

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


def _ensure_lfg_rsvp(message_id: int) -> dict[str, set[int]]:
    data = LFG_RSVP.setdefault(message_id, {"join": set(), "maybe": set(), "pass": set()})
    for key in ("join", "maybe", "pass"):
        data.setdefault(key, set())
    return data


def _lfg_rsvp_text(message_id: int, players_needed: int) -> str:
    data = _ensure_lfg_rsvp(message_id)
    going = len(data["join"])
    maybe = len(data["maybe"])
    passing = len(data["pass"])
    return (
        f"✅ Going: **{going}/{players_needed}**\n"
        f"❔ Maybe: **{maybe}**\n"
        f"❌ Pass: **{passing}**"
    )


_load_posted_games()


def _load_prestige_data() -> None:
    global PRESTIGE_DATA
    if not os.path.exists(_PRESTIGE_SAVE):
        return
    try:
        with open(_PRESTIGE_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            PRESTIGE_DATA = {int(uid): profile for uid, profile in data.items() if isinstance(profile, dict)}
    except Exception as e:
        print(f"[Prestige] Warning: could not load prestige data — {e}")


def _save_prestige_data() -> None:
    try:
        with open(_PRESTIGE_SAVE, "w", encoding="utf-8") as f:
            json.dump(PRESTIGE_DATA, f)
    except Exception as e:
        print(f"[Prestige] Warning: could not save prestige data — {e}")


def _load_cosmetics_inventory() -> None:
    global PLAYER_COSMETICS
    if not os.path.exists(_COSMETICS_SAVE):
        return
    try:
        with open(_COSMETICS_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            PLAYER_COSMETICS = {int(uid): set(items) for uid, items in data.items() if isinstance(items, list)}
    except Exception as e:
        print(f"[Cosmetics] Warning: could not load cosmetics — {e}")


def _save_cosmetics_inventory() -> None:
    try:
        with open(_COSMETICS_SAVE, "w", encoding="utf-8") as f:
            json.dump({uid: list(items) for uid, items in PLAYER_COSMETICS.items()}, f)
    except Exception as e:
        print(f"[Cosmetics] Warning: could not save cosmetics — {e}")


def _prestige_profile(user_id: int) -> dict[str, int]:
    profile = PRESTIGE_DATA.setdefault(user_id, {"prestige": 0, "resets": 0, "total_xp_earned": 0})
    return profile


def _get_prestige(user_id: int) -> int:
    return _prestige_profile(user_id).get("prestige", 0)


def _xp_multiplier(user_id: int) -> float:
    prestige = _get_prestige(user_id)
    return 1.0 + (prestige * PRESTIGE_BONUS_XP_PER_LEVEL)


def _load_link_quarantine() -> None:
    """Load link quarantine data from disk."""
    global LINK_QUARANTINE
    if not os.path.exists(_QUARANTINE_SAVE):
        return
    try:
        with open(_QUARANTINE_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            LINK_QUARANTINE = {int(uid): record for uid, record in data.items() if isinstance(record, dict)}
    except Exception as e:
        print(f"[LinkQuarantine] Warning: could not load quarantine data — {e}")


def _save_link_quarantine() -> None:
    """Persist link quarantine data to disk."""
    try:
        with open(_QUARANTINE_SAVE, "w", encoding="utf-8") as f:
            json.dump(LINK_QUARANTINE, f)
    except Exception as e:
        print(f"[LinkQuarantine] Warning: could not save quarantine data — {e}")


_load_prestige_data()
_load_cosmetics_inventory()
_load_link_quarantine()

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
        # Allow close if: has manage_channels, OR in OPEN_TICKETS (in-memory),
        # OR has an explicit view_channel overwrite (ticket opener after bot restart).
        user_ow = ch.overwrites_for(interaction.user)
        can_close = (
            interaction.user.guild_permissions.manage_channels
            or OPEN_TICKETS.get(interaction.user.id) == ch.id
            or user_ow.view_channel is True
        )
        if not can_close:
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Closing ticket in 5 seconds...")
        # log transcript
        log_ch = _resolve_ticket_log_channel(interaction.guild)
        if log_ch is None:
            print(f"[Tickets] WARNING: #{TICKET_LOG_NAME} channel not found — transcript not saved for {ch.name}")
        else:
            try:
                msgs = [m async for m in ch.history(limit=200, oldest_first=True)]
                transcript = "\n".join(
                    f"[{m.created_at.strftime('%H:%M:%S')}] {m.author}: {m.content}"
                    for m in msgs if not m.author.bot or m.content
                )
                embed = discord.Embed(title=f"📋 Ticket Closed — #{ch.name}", color=0xE74C3C)
                embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
                embed.description = f"```\n{transcript[:3900]}\n```" if transcript else "*No messages.*"
                embed.timestamp = discord.utils.utcnow()
                await log_ch.send(embed=embed)
            except Exception as e:
                print(f"[Tickets] ERROR logging transcript for {ch.name}: {e}")
        # remove from open tickets
        for uid, cid in list(OPEN_TICKETS.items()):
            if cid == ch.id:
                del OPEN_TICKETS[uid]
                break
        TICKET_SLA_LAST_REMINDER.pop(ch.id, None)
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
        # Defer immediately so Discord doesn't time out during API calls.
        await interaction.response.defer(ephemeral=True)

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            # Cache miss — fetch from API directly.
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except Exception:
                await interaction.followup.send(
                    "❌ Could not find your member record. Please try again in a moment.", ephemeral=True
                )
                return
        gamer_role = discord.utils.get(interaction.guild.roles, name=GAMER_ROLE_NAME)
        if not gamer_role:
            await interaction.followup.send(
                "❌ The Gamer role doesn't exist yet — ask an admin to run `/setupchannels`.",
                ephemeral=True,
            )
            return
        if gamer_role in member.roles:
            await interaction.followup.send(
                "✅ You're already verified and have full access!", ephemeral=True
            )
            return
        try:
            await member.add_roles(gamer_role, reason="Self-verification via embed button")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to assign roles. Ask an admin to move my bot role **above** the Gamer role in Server Settings → Roles.",
                ephemeral=True,
            )
            print(f"[Verify] FORBIDDEN — bot role is below @{GAMER_ROLE_NAME} in hierarchy. Cannot assign role.")
            return
        except Exception as e:
            await interaction.followup.send(
                "❌ Something went wrong assigning your role. Please try again or contact an admin.", ephemeral=True
            )
            print(f"[Verify] ERROR assigning @{GAMER_ROLE_NAME} to {member}: {e}")
            return
        try:
            await log_role_change(interaction.guild, member, gamer_role, added=True, source="Verification Embed")
        except Exception as e:
            print(f"[Verify] WARNING: failed to log role change: {e}")

        reward_text = ""
        try:
            pokemon_game._ensure_player(member.id)
            pokemon_game.WALLETS[member.id] = pokemon_game._wallet(member.id) + VERIFY_REWARD_COINS
            new_balance = pokemon_game.WALLETS[member.id]
            reward_text = (
                f"\n💰 You were awarded **{VERIFY_REWARD_COINS} PokeCoins** for verifying! "
                f"New balance: **{new_balance:,}**."
            )

            log_ch = _resolve_or_track_text_channel(interaction.guild, "bot_log", BOT_LOG_NAME, "bot-logs")
            if not log_ch:
                log_ch = _resolve_mod_log_channel(interaction.guild)
            if log_ch:
                coin_embed = discord.Embed(title="💰 PokeCoin Event", color=0x2ECC71)
                coin_embed.add_field(name="Recipient", value=f"{member.mention} (`{member.id}`)", inline=True)
                coin_embed.add_field(name="Amount", value=f"`+{VERIFY_REWARD_COINS:,}` PokeCoins", inline=True)
                coin_embed.add_field(name="Balance", value=f"`{new_balance:,}` PokeCoins", inline=True)
                coin_embed.add_field(name="Source", value="verify reward", inline=False)
                coin_embed.timestamp = discord.utils.utcnow()
                await log_ch.send(embed=coin_embed)
        except Exception as e:
            print(f"[Verify] WARNING: failed to award verification coins: {e}")

        await interaction.followup.send(
            f"🎉 Welcome! You now have the **@{GAMER_ROLE_NAME}** role and can access all channels!{reward_text}",
            ephemeral=True,
        )

class GamerVerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(GamerVerifyButton())


def _update_lfg_embed_rsvp(embed: discord.Embed, message_id: int, players_needed: int) -> None:
    text = _lfg_rsvp_text(message_id, players_needed)
    for idx, field in enumerate(embed.fields):
        if field.name == "RSVP":
            embed.set_field_at(idx, name="RSVP", value=text, inline=False)
            return
    embed.add_field(name="RSVP", value=text, inline=False)


class LFGRSVPView(discord.ui.View):
    def __init__(self, host_id: int, players_needed: int):
        super().__init__(timeout=86400)
        self.host_id = host_id
        self.players_needed = players_needed
        self.message_id: int | None = None

    async def _apply_choice(self, interaction: discord.Interaction, choice: str):
        if self.message_id is None:
            await interaction.response.send_message("This LFG session is not active anymore.", ephemeral=True)
            return
        post = LFG_POSTS.get(self.message_id)
        if not post or not post.get("active", True):
            await interaction.response.send_message("This LFG post is already closed.", ephemeral=True)
            return

        data = _ensure_lfg_rsvp(self.message_id)
        uid = interaction.user.id
        for key in ("join", "maybe", "pass"):
            data[key].discard(uid)
        data[choice].add(uid)

        message = interaction.message
        if message and message.embeds:
            embed = message.embeds[0]
            _update_lfg_embed_rsvp(embed, self.message_id, self.players_needed)
            await message.edit(embed=embed, view=self)

        labels = {"join": "You're marked as **Going**.", "maybe": "You're marked as **Maybe**.", "pass": "You're marked as **Pass**."}
        await interaction.response.send_message(labels[choice], ephemeral=True)

    @discord.ui.button(label="Going", emoji="✅", style=discord.ButtonStyle.success)
    async def btn_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_choice(interaction, "join")

    @discord.ui.button(label="Maybe", emoji="❔", style=discord.ButtonStyle.secondary)
    async def btn_maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_choice(interaction, "maybe")

    @discord.ui.button(label="Pass", emoji="❌", style=discord.ButtonStyle.secondary)
    async def btn_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_choice(interaction, "pass")

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.message_id is None:
            await interaction.response.send_message("This LFG session is not active anymore.", ephemeral=True)
            return
        if interaction.user.id != self.host_id and not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("Only the host (or a moderator) can close this LFG post.", ephemeral=True)
            return

        post = LFG_POSTS.get(self.message_id)
        if post:
            post["active"] = False

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        message = interaction.message
        if message and message.embeds:
            embed = message.embeds[0]
            embed.color = 0x7F8C8D
            embed.set_footer(text="LFG closed")
            await message.edit(embed=embed, view=self)
        await interaction.response.send_message("LFG closed.", ephemeral=True)


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
                    TICKET_SLA_LAST_REMINDER.pop(existing.id, None)
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
            TICKET_SLA_LAST_REMINDER.pop(ch.id, None)
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
@tasks.loop(minutes=10)
async def ticket_sla_check():
    """Alert staff when tickets remain open beyond SLA threshold."""
    now_unix = time.time()
    now_dt = discord.utils.utcnow()

    # Keep map clean by removing reminders for ticket channels that no longer exist.
    live_ticket_channel_ids = set(OPEN_TICKETS.values())
    for cid in list(TICKET_SLA_LAST_REMINDER.keys()):
        if cid not in live_ticket_channel_ids:
            TICKET_SLA_LAST_REMINDER.pop(cid, None)

    guild = client.get_guild(PRIMARY_GUILD_ID)
    if guild is None:
        return

    ticket_log = _resolve_ticket_log_channel(guild)
    guild_tickets = [(uid, cid) for uid, cid in OPEN_TICKETS.items() if isinstance(guild.get_channel(cid), discord.TextChannel)]
    for owner_id, channel_id in guild_tickets:
        ch = guild.get_channel(channel_id)
        if not isinstance(ch, discord.TextChannel):
            continue

        age_hours = (now_dt - ch.created_at).total_seconds() / 3600.0
        if age_hours < TICKET_SLA_HOURS:
            continue

        last = TICKET_SLA_LAST_REMINDER.get(channel_id, 0)
        if now_unix - last < TICKET_SLA_REMINDER_COOLDOWN_MINUTES * 60:
            continue

        TICKET_SLA_LAST_REMINDER[channel_id] = now_unix

        staff_mentions = [r.mention for r in guild.roles if r.permissions.manage_channels and not r.is_default()][:3]
        ping = " ".join(staff_mentions) if staff_mentions else "@here"
        age_txt = f"{age_hours:.1f}h"

        try:
            await ch.send(
                f"⏰ **Ticket SLA Reminder**: this ticket has been open for **{age_txt}**. {ping}\n"
                f"Opened by <@{owner_id}>."
            )
        except Exception as e:
            print(f"[TicketSLA] Could not post reminder in #{ch.name}: {e}")

        if ticket_log:
            try:
                embed = discord.Embed(title="⏰ Ticket SLA Reminder", color=0xF39C12)
                embed.add_field(name="Ticket", value=ch.mention, inline=True)
                embed.add_field(name="Opened By", value=f"<@{owner_id}>", inline=True)
                embed.add_field(name="Open Time", value=age_txt, inline=True)
                embed.timestamp = now_dt
                await ticket_log.send(embed=embed)
            except Exception as e:
                print(f"[TicketSLA] Could not log SLA reminder for #{ch.name}: {e}")


@tasks.loop(minutes=2)
async def empty_vc_cleanup():
    """Scan all guilds for empty user-created voice channels and delete them."""
    guild = client.get_guild(PRIMARY_GUILD_ID)
    if guild is None:
        return
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


async def _recent_announced_free_game_urls(ch: discord.TextChannel, limit: int = 250) -> set[str]:
    """Collect recently posted free-game URLs from bot embeds in channel history."""
    announced: set[str] = set()
    try:
        async for msg in ch.history(limit=limit):
            if msg.author != ch.guild.me:
                continue
            for emb in msg.embeds:
                if emb.url:
                    announced.add(emb.url.strip())
    except Exception as e:
        print(f"[FreeGames] Could not read channel history for dedupe: {e}")
    return announced


@tasks.loop(hours=4)
async def free_games_check():
    global FREE_GAMES_FIRST_CYCLE
    guild = client.get_guild(GUILD_ID.id)
    if not guild:
        return

    # Skip first cycle after boot to avoid restart repost storms.
    if FREE_GAMES_FIRST_CYCLE:
        FREE_GAMES_FIRST_CYCLE = False
        print("[FreeGames] First cycle after startup skipped.")
        return

    # Background loop should not create channels; only post if target channel exists.
    ch = _resolve_or_track_text_channel(guild, "free_games_channel", FREE_GAMES_CHANNEL_NAME, "freegames", "free-games")
    if not ch:
        print(f"[FreeGames] Channel #{FREE_GAMES_CHANNEL_NAME} not found in {guild.name}; skipping cycle.")
        return

    loop = asyncio.get_running_loop()
    games = await loop.run_in_executor(None, _fetch_steam_free_games)
    announced_urls = await _recent_announced_free_game_urls(ch)

    new_games = [
        g for g in games
        if g["id"] not in POSTED_FREE_GAMES and g.get("url", "").strip() not in announced_urls
    ]
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

        # ── Phase 4: Raid Mode (lockdown) ──────────────────────────────────────
        # During raid mode, only mods/admins can message
        gid = message.guild.id
        if RAID_MODE_ACTIVE.get(gid, False):
            if not (message.author.guild_permissions.administrator or message.author.guild_permissions.moderate_members):
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                notice = await message.channel.send(
                    f"{message.author.mention} 🔒 **Raid mode is active.** Only moderators can message during this time.",
                    delete_after=5,
                )
                return

        # ── Phase 4: Account Age Gate ──────────────────────────────────────────
        # Prevent new accounts from posting certain content
        if ACCOUNT_AGE_GATE_ENABLED.get(gid, False):
            account_age = (discord.utils.utcnow() - message.author.created_at).total_seconds() / 86400
            if account_age < ACCOUNT_AGE_GATE_DAYS:
                # Check for links or suspicious content
                if "http://" in message.content or "https://" in message.content or "discord.gg" in message.content:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                    notice = await message.channel.send(
                        f"{message.author.mention} ⏳ New accounts ({ACCOUNT_AGE_GATE_DAYS}+ days required) cannot post external links. "
                        f"Your account is {account_age:.1f} days old.",
                        delete_after=7,
                    )
                    log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        embed = discord.Embed(title="🔐 Auto-Mod: Account Age Gate (Link Blocked)", color=0x3498DB)
                        embed.add_field(name="User", value=f"{message.author.mention} ({message.author})", inline=False)
                        embed.add_field(name="Account Age (days)", value=f"{account_age:.1f}", inline=False)
                        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                        embed.timestamp = discord.utils.utcnow()
                        await log_ch.send(embed=embed)
                    return

        # ── Phase 4: Link Quarantine ───────────────────────────────────────────
        # Detect and quarantine suspicious non-Discord links
        _SAFE_DOMAINS = {"youtube.com", "youtu.be", "twitch.tv", "github.com", "imgur.com", "tenor.com"}
        if "http://" in message.content or "https://" in message.content:
            links = re.findall(r'https?://([^\s/]+)', message.content)
            suspicious_links = [link for link in links if link not in _SAFE_DOMAINS and "discord" not in link.lower()]
            
            if suspicious_links:
                record = LINK_QUARANTINE.setdefault(message.author.id, {"quarantine_level": 0, "last_violation_time": 0})
                record["last_violation_time"] = time.time()
                record["quarantine_level"] = min(record["quarantine_level"] + 1, 2)
                _save_link_quarantine()
                
                q_level = record["quarantine_level"]
                if q_level >= 2:
                    # Level 2: ban user
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                    try:
                        await message.author.ban(reason="Auto-mod: suspicious link (quarantine level 2)")
                    except discord.HTTPException:
                        pass
                    notice = await message.channel.send(
                        f"{message.author.mention} 🚫 You have been **banned** for repeated suspicious link posting.",
                        delete_after=5,
                    )
                    log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        embed = discord.Embed(title="🚫 Auto-Mod: Banned (Suspicious Links - Level 2)", color=0x992D22)
                        embed.add_field(name="User", value=f"{message.author.mention} ({message.author})", inline=False)
                        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                        embed.add_field(name="Links Detected", value=", ".join(suspicious_links[:5]), inline=False)
                        embed.timestamp = discord.utils.utcnow()
                        await log_ch.send(embed=embed)
                elif q_level == 1:
                    # Level 1: quarantine (delete message, warn)
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                    warning = await message.channel.send(
                        f"{message.author.mention} ⚠️ **Message removed:** Suspicious link detected (requires staff review). "
                        f"A second offense will result in a **ban**.",
                        delete_after=8,
                    )
                    log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        embed = discord.Embed(title="⚠️ Auto-Mod: Suspicious Link Detected (Level 1)", color=0xFF6B6B)
                        embed.add_field(name="User", value=f"{message.author.mention} ({message.author})", inline=False)
                        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                        embed.add_field(name="Links Detected", value=", ".join(suspicious_links[:5]), inline=False)
                        embed.timestamp = discord.utils.utcnow()
                        await log_ch.send(embed=embed)
                return


async def get_mod_log_channel(guild: discord.Guild):
    channel = _resolve_mod_log_channel(guild)
    if channel is None:
        print(f"Mod log channel not found (ID {LOG_CHANNEL_ID} / name {MOD_LOG_NAME}).")
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
    mod_log = _resolve_mod_log_channel(interaction.guild)
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
    ch = _resolve_mod_log_channel(interaction.guild)
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

# ── Phase 4: Moderation Safety (Raid Mode, Link Quarantine, Account Age Gate) ──
@client.tree.command(name="raidmode", description="Toggle raid mode lockdown (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def raidmode(interaction: discord.Interaction, enabled: bool):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    gid = interaction.guild.id
    RAID_MODE_ACTIVE[gid] = enabled
    status = "🔒 **enabled**" if enabled else "🔓 **disabled**"
    embed = discord.Embed(
        title="Raid Mode",
        description=f"Raid mode is now {status}.",
        color=0xFF6B6B if enabled else 0x2ECC71
    )
    if enabled:
        embed.add_field(name="Effect", value="Only moderators and admins can send messages.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await log_action(interaction, "Raid Mode", f"Status: {'Enabled' if enabled else 'Disabled'}")

@client.tree.command(name="accountage", description="Set minimum account age gate (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def accountage(interaction: discord.Interaction, days: int, enabled: bool = True):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    if days < 0 or days > 365:
        await interaction.response.send_message("Account age must be between 0 and 365 days.", ephemeral=True)
        return
    gid = interaction.guild.id
    ACCOUNT_AGE_GATE_ENABLED[gid] = enabled
    global ACCOUNT_AGE_GATE_DAYS
    ACCOUNT_AGE_GATE_DAYS = days
    status = "**enabled**" if enabled else "**disabled**"
    embed = discord.Embed(
        title="Account Age Gate",
        description=f"Account age gate is now {status} (minimum: **{days} days**).",
        color=0x3498DB if enabled else 0x95A5A6
    )
    if enabled:
        embed.add_field(name="Effect", value=f"Accounts younger than {days} days cannot post external links.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await log_action(interaction, "Account Age Gate", f"Days: {days}, Status: {'Enabled' if enabled else 'Disabled'}")

@client.tree.command(name="linkquarantine", description="View or clear link quarantine records (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def linkquarantine(interaction: discord.Interaction, action: str = "view", user_id: str = ""):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    
    if action.lower() == "view":
        if not LINK_QUARANTINE:
            await interaction.response.send_message("No users in quarantine.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="🔐 Link Quarantine Records", color=0xFF6B6B)
        lines = []
        for uid, record in sorted(LINK_QUARANTINE.items()):
            q_level = record.get("quarantine_level", 0)
            level_text = {0: "None", 1: "⚠️ Quarantine", 2: "🚫 Banned"}
            lines.append(f"`{uid}` — {level_text.get(q_level, 'Unknown')}")
        embed.description = "\n".join(lines[:25]) if lines else "None"
        if len(lines) > 25:
            embed.set_footer(text=f"...and {len(lines) - 25} more")
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    elif action.lower() == "clear":
        if not user_id:
            await interaction.response.send_message("Specify a user ID to clear (e.g., `/linkquarantine clear 123456`)", ephemeral=True)
            return
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message("Invalid user ID.", ephemeral=True)
            return
        if uid not in LINK_QUARANTINE:
            await interaction.response.send_message(f"User `{uid}` is not in quarantine.", ephemeral=True)
            return
        LINK_QUARANTINE.pop(uid, None)
        _save_link_quarantine()
        await interaction.response.send_message(f"✅ Cleared quarantine record for user `{uid}`.", ephemeral=True)
        await log_action(interaction, "Link Quarantine Clear", f"User ID: {uid}")
    else:
        await interaction.response.send_message("Action must be `view` or `clear`.", ephemeral=True)

# ── Music System ─────────────────────────────────────────────────────────────

from pytubefix import Search, YouTube as PyTube

import platform as _platform
import shutil as _shutil


def _is_usable_ffmpeg(path: str | None) -> bool:
    if not path:
        return False
    if not os.path.exists(path):
        return False
    try:
        import subprocess
        subprocess.run([path, "-version"], capture_output=True, text=True, timeout=8, check=False)
        return True
    except Exception:
        return False


def _resolve_ffmpeg_executable() -> str:
    if _platform.system() == "Windows":
        return (
            r"C:\Users\kobec\AppData\Local\Microsoft\WinGet\Packages"
            r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            r"\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
        )

    for candidate in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"):
        if _is_usable_ffmpeg(candidate):
            return candidate

    system_ffmpeg = _shutil.which("ffmpeg")
    if _is_usable_ffmpeg(system_ffmpeg):
        return system_ffmpeg

    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
        bundled_ffmpeg = _shutil.which("ffmpeg")
        if _is_usable_ffmpeg(bundled_ffmpeg):
            return bundled_ffmpeg
        try:
            # Ensure binaries are fetched if PATH injection alone didn't expose ffmpeg.
            from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise
            ffmpeg_path, _ = get_or_fetch_platform_executables_else_raise()
            if _is_usable_ffmpeg(ffmpeg_path):
                print(f"[Music] Using static-ffmpeg binary: {ffmpeg_path}")
                return ffmpeg_path
        except Exception as fetch_err:
            print(f"[Music] static-ffmpeg fetch failed: {fetch_err}")
    except Exception as e:
        print(f"[Music] static-ffmpeg fallback unavailable: {e}")

    try:
        import imageio_ffmpeg
        imageio_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if _is_usable_ffmpeg(imageio_exe):
            print(f"[Music] Using imageio-ffmpeg binary: {imageio_exe}")
            return imageio_exe
    except Exception as e:
        print(f"[Music] imageio-ffmpeg fallback unavailable: {e}")

    env_ffmpeg = os.getenv("FFMPEG_PATH", "").strip()
    if _is_usable_ffmpeg(env_ffmpeg):
        return env_ffmpeg

    print(f"[Music] FFmpeg resolution failed. PATH={os.getenv('PATH', '')}")
    return "ffmpeg"

if _platform.system() == "Windows":
    FFMPEG_EXE = _resolve_ffmpeg_executable()
else:
    FFMPEG_EXE = _resolve_ffmpeg_executable()
print(f"[Music] Using FFmpeg executable: {FFMPEG_EXE}")
print("[Music] Build marker: deploy-temp e3891c3+diag")

if FFMPEG_EXE == "ffmpeg" and not _shutil.which("ffmpeg"):
    print("[Music] WARNING: ffmpeg binary not available. Set FFMPEG_PATH or ensure apt package installation on Railway.")

FFMPEG_OPTS = {
    'executable': FFMPEG_EXE,
    'before_options': '-nostdin -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -q:a 5 -ac 2 -ar 48000',
}

YTDL_STREAM_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'format': 'bestaudio/best',
    'extractor_args': {'youtube': {'player_client': ['android', 'web', 'tv_embedded']}},
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
        self.last_finished: SongEntry | None = None
        self.recent_track_ids: collections.deque[str] = collections.deque(maxlen=20)
        self.recent_title_keys: collections.deque[str] = collections.deque(maxlen=20)
        self.recent_artist_keys: collections.deque[str] = collections.deque(maxlen=12)
        self.voice_client: discord.VoiceClient | None = None
        self.volume: float = 0.5
        self.now_playing_msg: discord.Message | None = None
        self.autoplay: bool = False

music_states: dict[int, GuildMusicState] = {}

def get_music_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]


def _youtube_video_id(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            return parsed.path.lstrip("/")
        if "youtube.com" in host:
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("v"):
                return q["v"][0]
    except Exception:
        return ""
    return ""


def _song_identity(song: SongEntry | None) -> str:
    if not song:
        return ""
    return _youtube_video_id(song.webpage_url) or _youtube_video_id(song.url)


def _entry_identity(entry: dict) -> str:
    return _youtube_video_id(entry.get("webpage_url", "")) or _youtube_video_id(entry.get("url", ""))


def _normalized_title_key(title: str) -> str:
    """Normalize song titles so variants like remaster/official-audio map together."""
    t = (title or "").lower()
    # Remove bracketed noise first.
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    # Remove common noisy descriptors.
    noise = [
        "official audio", "official video", "official music video", "music video",
        "lyrics", "lyric video", "audio", "video", "remaster", "remastered",
        "hq", "hd", "topic",
    ]
    for n in noise:
        t = t.replace(n, " ")
    # Remove years and separators.
    t = re.sub(r"\b(?:19|20)\d{2}\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _song_core_key(title: str) -> str:
    """Best-effort core track key, stripping artist-prefix formats like 'Artist - Song'."""
    raw = (title or "").lower()
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"\[[^\]]*\]", " ", raw)

    # Common YouTube style: "Artist - Song"
    if " - " in raw:
        rhs = raw.split(" - ", 1)[1].strip()
        rhs_key = _normalized_title_key(rhs)
        if rhs_key:
            return rhs_key

    # "Song by Artist"
    if " by " in raw:
        lhs = raw.split(" by ", 1)[0].strip()
        lhs_key = _normalized_title_key(lhs)
        if lhs_key:
            return lhs_key

    return _normalized_title_key(raw)


def _same_song_key(a: str, b: str) -> bool:
    """Treat keys as same song if equal or one clearly contains the other."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = min(len(a), len(b))
    return shorter >= 6 and (a in b or b in a)


def _titles_too_similar(seed_title: str, candidate_title: str) -> bool:
    """Heuristic guard against same-song variants with slightly different titles."""
    a = _song_core_key(seed_title)
    b = _song_core_key(candidate_title)
    if not a or not b:
        return False
    if _same_song_key(a, b):
        return True

    ta = [t for t in a.split() if t]
    tb = [t for t in b.split() if t]
    if not ta or not tb:
        return False

    overlap = len(set(ta).intersection(tb))
    min_len = max(1, min(len(ta), len(tb)))
    overlap_ratio = overlap / min_len
    # High token overlap usually means remaster/intro/live variants of the same song.
    if overlap >= 2 and overlap_ratio >= 0.75:
        return True

    # Same leading phrase often indicates the same song with variant suffixes.
    lead_a = " ".join(ta[:2])
    lead_b = " ".join(tb[:2])
    if len(lead_a) >= 6 and lead_a == lead_b:
        return True

    return False


def _song_signature_tokens(title: str) -> list[str]:
    """Core tokens used to prevent same-song remixes/covers from slipping in."""
    core = _song_core_key(title)
    if not core:
        return []
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with"}
    tokens = [t for t in core.split() if len(t) >= 3 and t not in stop]
    # keep first few meaningful tokens as the song signature
    return tokens[:4]


def _artist_key_from_title(title: str) -> str:
    """Best-effort artist extraction from common title formats."""
    raw = (title or "").lower()
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"\[[^\]]*\]", " ", raw)

    artist_part = ""
    # Common style: "Artist - Song"
    if " - " in raw:
        artist_part = raw.split(" - ", 1)[0].strip()
    # Common style: "Song by Artist"
    elif " by " in raw:
        artist_part = raw.split(" by ", 1)[1].strip()

    # Keep only a clean leading artist token block.
    artist_part = artist_part.split("|")[0].split("/")[0].split(",")[0].strip()
    artist_part = re.sub(r"\b(feat|ft|featuring|official|topic|vevo)\b.*$", "", artist_part).strip()
    artist_part = re.sub(r"[^a-z0-9]+", " ", artist_part)
    artist_part = re.sub(r"\s+", " ", artist_part).strip()
    return artist_part


def _song_artist_key(song: SongEntry | None) -> str:
    if not song:
        return ""
    return _artist_key_from_title(song.title)


def _entry_artist_key(entry: dict) -> str:
    return _artist_key_from_title(entry.get("title", ""))


def _remember_finished_song(state: GuildMusicState, song: SongEntry | None) -> None:
    if not song:
        return
    state.last_finished = song
    sid = _song_identity(song)
    if sid:
        state.recent_track_ids.append(sid)
    tkey = _song_core_key(song.title)
    if tkey:
        state.recent_title_keys.append(tkey)
    akey = _song_artist_key(song)
    if akey:
        state.recent_artist_keys.append(akey)

def _pytubefix_search(query: str, max_results: int) -> list[dict]:
    """Try pytubefix first; fall back to yt-dlp and Invidious if blocked."""
    try:
        results = Search(query)
    except Exception as e:
        print(f'[Search Error] pytubefix failed: {e}. Trying yt-dlp...')
        return _ytdlp_search(query, max_results)

    entries = []
    for yt in results.videos[:max_results]:
        try:
            stream = yt.streams.filter(only_audio=True).order_by('abr').last()
            if stream:
                entries.append({
                    'title': yt.title,
                    'url': stream.url,
                    'webpage_url': yt.watch_url,
                    'duration': yt.length or 0,
                })
        except Exception as e:
            print(f'[Search] pytubefix item failed: {e}')
            continue

    if entries:
        return entries
    return _ytdlp_search(query, max_results)


def _ytdlp_search(query: str, max_results: int) -> list[dict]:
    """Fallback: use yt-dlp search and resolve a playable audio URL."""
    try:
        import yt_dlp

        cookiefile = os.getenv("YTDLP_COOKIES_PATH", "").strip()
        base_opts = {
            'quiet': True,
            'no_warnings': True,
            'default_search': 'ytsearch',
            'format': 'bestaudio/best',
            'noplaylist': True,
            'http_headers': {'User-Agent': 'Mozilla/5.0'},
            'source_address': '0.0.0.0',
        }
        if cookiefile and os.path.exists(cookiefile):
            base_opts['cookiefile'] = cookiefile
            print(f"[yt-dlp] Using cookie file: {cookiefile}")

        attempts = [
            {
                **base_opts,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            },
            {
                **base_opts,
                'extractor_args': {'youtube': {'player_client': ['ios', 'android']}},
            },
            {
                **base_opts,
                'extractor_args': {'youtube': {'player_client': ['tv_embedded', 'web']}},
            },
        ]

        queries = [
            f"ytsearch{max_results}:{query}",
            f"ytsearch{max_results}:{query} audio",
        ]

        for ydl_opts in attempts:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    for q in queries:
                        try:
                            info = ydl.extract_info(q, download=False)
                            entries = []
                            for entry in info.get('entries', [])[:max_results]:
                                if not entry:
                                    continue
                                try:
                                    stream_url = entry.get('url')
                                    webpage_url = entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
                                    if not stream_url or 'youtube.com/watch' in str(stream_url):
                                        resolved = ydl.extract_info(webpage_url, download=False)
                                        stream_url = resolved.get('url')
                                    if not stream_url:
                                        continue

                                    entries.append({
                                        'title': entry.get('title', 'Unknown title'),
                                        'url': stream_url,
                                        'webpage_url': webpage_url,
                                        'duration': entry.get('duration', 0),
                                    })
                                except Exception as e:
                                    print(f'[yt-dlp] entry failed: {e}')
                                    continue

                            if entries:
                                return entries
                        except Exception as e:
                            print(f'[yt-dlp] query failed ({q}): {e}')
                            continue
            except Exception as e:
                print(f'[yt-dlp] attempt failed: {e}')
                continue

        return _invidious_search(query, max_results)
    except Exception as e:
        print(f'[yt-dlp] failed: {e}')
        return _invidious_search(query, max_results)


def _invidious_search(query: str, max_results: int) -> list[dict]:
    """Last fallback using public Invidious instances."""
    instances = [
        'https://inv.nadeko.net',
        'https://invidious.privacyredirect.com',
        'https://invidious.fdn.fr',
        'https://invidious.projectsegfau.lt',
        'https://yewtu.be',
    ]

    for base in instances:
        try:
            search_url = (
                f"{base}/api/v1/search?q={urllib.parse.quote(query)}"
                f"&type=video&sort_by=relevance"
            )
            req = urllib.request.Request(
                search_url,
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Accept': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode(errors='ignore'))

            entries = []
            for item in data:
                if item.get('type') != 'video':
                    continue
                vid = item.get('videoId')
                if not vid:
                    continue

                try:
                    details_url = f"{base}/api/v1/videos/{vid}"
                    req2 = urllib.request.Request(
                        details_url,
                        headers={
                            'User-Agent': 'Mozilla/5.0',
                            'Accept': 'application/json',
                        },
                    )
                    with urllib.request.urlopen(req2, timeout=6) as resp2:
                        details = json.loads(resp2.read().decode(errors='ignore'))

                    audio_formats = [
                        f for f in details.get('adaptiveFormats', [])
                        if 'audio' in str(f.get('type', '')).lower() and f.get('url')
                    ]
                    if not audio_formats:
                        audio_formats = [
                            f for f in details.get('formatStreams', [])
                            if 'audio' in str(f.get('type', '')).lower() and f.get('url')
                        ]
                    if not audio_formats:
                        continue
                    best_audio = max(audio_formats, key=lambda f: int(f.get('bitrate', 0) or 0))

                    entries.append({
                        'title': item.get('title', 'Unknown title'),
                        'url': best_audio['url'],
                        'webpage_url': f"https://www.youtube.com/watch?v={vid}",
                        'duration': int(item.get('lengthSeconds') or 0),
                    })
                    if len(entries) >= max_results:
                        break
                except Exception as e:
                    print(f'[Invidious] resolve failed for {vid}: {e}')
                    continue

            if entries:
                return entries
        except Exception as e:
            print(f'[Invidious] instance failed {base}: {e}')
            continue

    return []

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


def _looks_like_url(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith("http://") or t.startswith("https://")


def _ytdlp_resolve_url(url: str) -> list[dict]:
    """Resolve a direct video URL to a playable audio stream using yt-dlp."""
    try:
        import yt_dlp

        cookiefile = os.getenv("YTDLP_COOKIES_PATH", "").strip()
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio/best',
            'noplaylist': True,
            'http_headers': {'User-Agent': 'Mozilla/5.0'},
            'source_address': '0.0.0.0',
        }
        if cookiefile and os.path.exists(cookiefile):
            ydl_opts['cookiefile'] = cookiefile

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            # Playlist-ish responses can still happen; use first entry if present.
            if info and info.get('entries'):
                info = next((e for e in info.get('entries', []) if e), None)
            if not info:
                return []

            stream_url = info.get('url')
            if not stream_url and info.get('webpage_url'):
                resolved = ydl.extract_info(info.get('webpage_url'), download=False)
                stream_url = resolved.get('url') if resolved else None
            if not stream_url:
                return []

            return [{
                'title': info.get('title', 'Unknown title'),
                'url': stream_url,
                'webpage_url': info.get('webpage_url') or url,
                'duration': info.get('duration', 0) or 0,
            }]
    except Exception as e:
        print(f"[yt-dlp] direct url resolve failed: {e}")
        return []


async def search_youtube_resilient(query: str, max_results: int = 1) -> list[dict]:
    """Resilient lookup for /play: direct URL resolve first, then fallback query variants."""
    q = (query or "").strip()
    if not q:
        return []

    loop = asyncio.get_running_loop()

    if _looks_like_url(q):
        # Normalize music.youtube/watch variants for better extractor compatibility.
        normalized = q.replace("music.youtube.com", "www.youtube.com")
        direct = await loop.run_in_executor(None, lambda: _ytdlp_resolve_url(normalized))
        if direct:
            return direct

    # Try original query, then common useful variants.
    attempts: list[str] = [q]
    if not _looks_like_url(q):
        attempts.extend([
            f"{q} official audio",
            f"{q} topic",
            f"{q} lyrics",
        ])

    seen: set[str] = set()
    for attempt in attempts:
        key = attempt.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        results = await search_youtube(attempt, max_results=max_results)
        if results:
            return results

    return []


def _fetch_related_yt_dlp(webpage_url: str, exclude_url: str) -> list[dict]:
    """Use yt-dlp to pull YouTube's recommended/related videos for a given watch URL.
    Returns a list of dicts with title/url/webpage_url/duration, skipping exclude_url."""
    try:
        import yt_dlp
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extract_flat': True,
            'noplaylist': True,
        }
        cookiefile = os.getenv("YTDLP_COOKIES_PATH", "").strip()
        if cookiefile and os.path.isfile(cookiefile):
            opts['cookiefile'] = cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
        related = info.get('related_videos') or []
        results = []
        for v in related:
            vid_id = v.get('id') or v.get('url', '')
            if not vid_id:
                continue
            wurl = f"https://www.youtube.com/watch?v={vid_id}"
            if wurl == exclude_url or exclude_url.endswith(vid_id):
                continue
            results.append({
                'title': v.get('title') or v.get('id', 'Unknown'),
                'url': wurl,
                'webpage_url': wurl,
                'duration': v.get('duration') or 0,
            })
            if len(results) >= 5:
                break
        return results
    except Exception as e:
        print(f"[Autoplay] yt-dlp related fetch failed: {e}")
        return []


async def fetch_related_song(state: "GuildMusicState", current: "SongEntry") -> dict | None:
    """Fetch a genuine related song for autoplay. Tries yt-dlp related videos first,
    then falls back to a 'mix similar to <title>' text search."""
    loop = asyncio.get_running_loop()
    exclude = current.webpage_url or current.url
    blocked_ids: set[str] = set(state.recent_track_ids)
    blocked_title_keys: set[str] = set(state.recent_title_keys)
    blocked_artist_keys: set[str] = set(state.recent_artist_keys)
    current_id = _song_identity(current)
    if current_id:
        blocked_ids.add(current_id)
    current_title_key = _song_core_key(current.title)
    if current_title_key:
        blocked_title_keys.add(current_title_key)
    current_artist_key = _song_artist_key(current)
    seed_tokens = _song_signature_tokens(current.title)
    if current_artist_key:
        blocked_artist_keys.add(current_artist_key)
    for q_item in state.queue:
        qid = _song_identity(q_item)
        if qid:
            blocked_ids.add(qid)
        qkey = _song_core_key(q_item.title)
        if qkey:
            blocked_title_keys.add(qkey)
        qartist = _song_artist_key(q_item)
        if qartist:
            blocked_artist_keys.add(qartist)

    def _candidate_allowed(entry: dict) -> bool:
        rid = _entry_identity(entry)
        if rid and rid in blocked_ids:
            return False

        rtitle = entry.get('title', '')
        rkey = _song_core_key(rtitle)
        if rkey and any(_same_song_key(rkey, bkey) for bkey in blocked_title_keys):
            return False
        if _titles_too_similar(current.title, rtitle):
            return False

        # Hard guard: if candidate still contains the seed song signature tokens,
        # treat it as the same song family (remix/cover/edit) and skip.
        if seed_tokens:
            rtokens = set(t for t in _song_core_key(rtitle).split() if t)
            if len(seed_tokens) >= 2 and all(t in rtokens for t in seed_tokens[:2]):
                return False
            if len(seed_tokens) >= 3 and sum(1 for t in seed_tokens[:3] if t in rtokens) >= 2:
                return False

        rartist = _entry_artist_key(entry)
        if rartist and rartist in blocked_artist_keys:
            return False

        if rtitle.lower() == current.title.lower():
            return False

        return True

    # 1) Try pulling YouTube related videos via yt-dlp
    if exclude:
        related = await loop.run_in_executor(None, lambda: _fetch_related_yt_dlp(exclude, exclude))
        for r in related:
            if _candidate_allowed(r):
                return r

    # 2) Fallback: search for "<title> mix" and skip exact title match
    try:
        results = await search_youtube(f"mix similar to {current.title}", max_results=10)
        for r in results:
            if _candidate_allowed(r):
                return r

        # 3) Broader diversification search when related/mix results are still too similar.
        broad_queries = [
            f"songs like {current.title}",
            f"music recommendations similar to {current.title}",
        ]
        if current_artist_key:
            broad_queries.append(f"artists similar to {current_artist_key} best songs")

        for q in broad_queries:
            picks = await search_youtube(q, max_results=12)
            for r in picks:
                if _candidate_allowed(r):
                    return r
    except Exception as e:
        print(f"[Autoplay] Fallback search failed: {e}")

    return None


def _ffmpeg_candidate_paths() -> list[str]:
    candidates: list[str] = []

    env_ffmpeg = os.getenv("FFMPEG_PATH", "").strip()
    if env_ffmpeg:
        candidates.append(env_ffmpeg)

    if FFMPEG_EXE:
        candidates.append(FFMPEG_EXE)

    for path in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"):
        candidates.append(path)

    wh = _shutil.which("ffmpeg")
    if wh:
        candidates.append(wh)

    try:
        import imageio_ffmpeg

        imageio_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if imageio_exe:
            candidates.append(imageio_exe)
    except Exception:
        pass

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


print(f"[Music] Startup FFmpeg candidates: {_ffmpeg_candidate_paths()}")


def _extract_stream_url(song: SongEntry) -> str | None:
    """Resolve a fresh playable audio URL right before playback."""
    target = song.webpage_url or song.url
    if not target:
        return None
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(YTDL_STREAM_OPTS) as ydl:
            info = ydl.extract_info(target, download=False)
            if info and info.get('url'):
                return info['url']
    except Exception as e:
        print(f"[Music] yt-dlp stream resolve failed for {song.title}: {e}")

    return song.url or None

def play_next(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    if state.queue and state.voice_client and state.voice_client.is_connected():
        state.current = state.queue.popleft()
        try:
            stream_url = _extract_stream_url(state.current)
            if not stream_url:
                raise RuntimeError("No playable stream URL could be resolved")

            audio = None
            last_error: Exception | None = None
            for ffmpeg_exe in _ffmpeg_candidate_paths():
                if ffmpeg_exe != "ffmpeg" and not os.path.exists(ffmpeg_exe):
                    continue
                try:
                    audio = discord.FFmpegPCMAudio(
                        stream_url,
                        executable=ffmpeg_exe,
                        before_options=FFMPEG_OPTS['before_options'],
                        options=FFMPEG_OPTS['options'],
                    )
                    print(f"[Music] Using FFmpeg candidate: {ffmpeg_exe}")
                    break
                except Exception as e:
                    last_error = e
                    print(f"[Music] FFmpeg candidate failed ({ffmpeg_exe}): {e}")

            if audio is None:
                raise RuntimeError(f"No working ffmpeg executable found. Last error: {last_error}")

            volume_factor = max(0.1, min(2.0, state.volume))
            source = discord.PCMVolumeTransformer(audio, volume=volume_factor)

            def after_play(error):
                if error:
                    print(f'[Music] Player error on "{state.current.title}": {error}')
                finished_song = state.current
                _remember_finished_song(state, finished_song)
                state.current = None
                _cleanup_song_file(finished_song)
                asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)

            print(f'[Music] Now playing: {state.current.title}')
            state.voice_client.play(source, after=after_play)
        except Exception as e:
            print(
                f"[Music] Failed to create audio source for {state.current.title}: {e} | "
                f"FFMPEG_EXE={FFMPEG_EXE} | "
                f"Candidates={_ffmpeg_candidate_paths()} | "
                f"PATH={os.getenv('PATH', '')}"
            )
            finished_song = state.current
            _remember_finished_song(state, finished_song)
            _cleanup_song_file(finished_song)
            state.current = None
            asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)
    else:
        _remember_finished_song(state, state.current)
        _cleanup_song_file(state.current)
        state.current = None

async def play_next_async(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    seed_song = state.current or state.last_finished
    # If queue is empty and autoplay is on, fetch a genuinely related song
    if not state.queue and state.autoplay and seed_song:
        try:
            r = await fetch_related_song(state, seed_song)
            if r:
                state.queue.append(SongEntry(
                    title=r['title'],
                    url=r['url'],
                    webpage_url=r['webpage_url'],
                    duration=r.get('duration') or 0,
                    requester=seed_song.requester,
                ))
                print(f"[Autoplay] Queued related: {r['title']}")
        except Exception as e:
            print(f"[Autoplay] Failed to queue related song: {e}")
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
        autoplay_note = "I'll queue related songs automatically!" if state.autoplay else ""
        await interaction.followup.send(
            f"🔁 Autoplay is now **{'ON' if state.autoplay else 'OFF'}**. {autoplay_note}",
            ephemeral=True
        )


class PaginatedHelpView(discord.ui.View):
    """Paginated help embeds with arrow buttons."""
    
    def __init__(self, embeds: list, author_id: int):
        super().__init__(timeout=60)
        self.embeds = embeds
        self.author_id = author_id
        self.current_page = 0
        self.update_buttons()
    
    def update_buttons(self):
        """Enable/disable arrow buttons based on current page."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "prev_btn":
                    item.disabled = self.current_page == 0
                elif item.custom_id == "next_btn":
                    item.disabled = self.current_page == len(self.embeds) - 1
    
    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary, custom_id="prev_btn")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
    
    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, custom_id="next_btn")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You can't use this button.", ephemeral=True)
            return
        if self.current_page < len(self.embeds) - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)


async def _post_music_panel(guild_id: int):
    """Delete the old Now Playing panel and post a fresh one with control buttons."""
    state = get_music_state(guild_id)
    guild = client.get_guild(guild_id)
    if not guild:
        return
    music_ch = _resolve_or_track_text_channel(guild, "music_channel", MUSIC_CHANNEL_NAME, "music-channel", "music")
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
    ch = _resolve_or_track_text_channel(
        interaction.guild,
        "music_channel",
        MUSIC_CHANNEL_NAME,
        "music-channel",
        "music",
    )
    return interaction.channel.id == (ch.id if ch else -1)

async def _require_music_channel(interaction: discord.Interaction) -> bool:
    """Sends an error and returns False if not in music-channel."""
    if not _is_music_channel(interaction):
        ch = _resolve_or_track_text_channel(
            interaction.guild,
            "music_channel",
            MUSIC_CHANNEL_NAME,
            "music-channel",
            "music",
        )
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

        results = await search_youtube_resilient(query, max_results=1)
        if not results:
            await interaction.followup.send(
                "No playable results found right now. Try a more specific title (song + artist), "
                "or paste a direct YouTube URL. If this keeps happening on Railway, add "
                "YTDLP_COOKIES_PATH to a valid cookies.txt file and retry."
            )
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
@app_commands.describe(
    game="Game title",
    players_needed="How many players you still need",
    description="Optional details (mode, rank, notes)",
    platform="Optional platform (PC, Xbox, PS5, etc.)",
    region="Optional region (NA, EU, OCE, etc.)",
    start_in_minutes="How soon you are starting",
    mic_required="Whether mic is required"
)
async def lfg(
    interaction: discord.Interaction,
    game: str,
    players_needed: int,
    description: str = "",
    platform: str = "",
    region: str = "",
    start_in_minutes: int = 0,
    mic_required: bool = False,
):
    if players_needed < 1 or players_needed > 20:
        await interaction.response.send_message("Players needed must be between 1 and 20.", ephemeral=True)
        return

    channel = discord.utils.get(interaction.guild.text_channels, name=LFG_CHANNEL_NAME)
    if not channel:
        try:
            channel = await interaction.guild.create_text_channel(LFG_CHANNEL_NAME, reason="LFG channel")
        except Exception:
            await interaction.response.send_message("Could not find or create a looking-for-group channel.", ephemeral=True)
            return

    embed = discord.Embed(title=f"🎮 LFG — {game}", color=0x00BFFF)
    embed.add_field(name="Host", value=interaction.user.mention, inline=True)
    embed.add_field(name="Players Needed", value=str(players_needed), inline=True)
    embed.add_field(name="Mic", value="Required" if mic_required else "Optional", inline=True)
    if platform.strip():
        embed.add_field(name="Platform", value=platform.strip(), inline=True)
    if region.strip():
        embed.add_field(name="Region", value=region.strip().upper(), inline=True)
    if start_in_minutes > 0:
        start_ts = int((discord.utils.utcnow() + datetime.timedelta(minutes=start_in_minutes)).timestamp())
        embed.add_field(name="Start Time", value=f"<t:{start_ts}:R>", inline=True)
    if description:
        embed.add_field(name="Details", value=description, inline=False)

    view = LFGRSVPView(host_id=interaction.user.id, players_needed=players_needed)
    msg = await channel.send(embed=embed, view=view)
    view.message_id = msg.id

    LFG_POSTS[msg.id] = {
        "guild_id": interaction.guild.id,
        "channel_id": channel.id,
        "host_id": interaction.user.id,
        "game": game.strip().lower(),
        "platform": platform.strip().lower(),
        "region": region.strip().lower(),
        "players_needed": players_needed,
        "active": True,
        "created_at": time.time(),
        "jump_url": msg.jump_url,
    }
    _ensure_lfg_rsvp(msg.id)

    embed = msg.embeds[0]
    _update_lfg_embed_rsvp(embed, msg.id, players_needed)
    await msg.edit(embed=embed, view=view)

    await interaction.response.send_message(f"LFG post created in {channel.mention}!", ephemeral=True)


@client.tree.command(name="lfgfilter", description="Browse active LFG posts with filters", guild=GUILD_ID)
@app_commands.describe(
    game="Filter by game title (optional)",
    platform="Filter by platform (optional)",
    region="Filter by region (optional)",
    host="Filter by host (optional)",
)
async def lfgfilter(
    interaction: discord.Interaction,
    game: str = "",
    platform: str = "",
    region: str = "",
    host: discord.Member = None,
):
    gid = interaction.guild.id
    game_q = game.strip().lower()
    platform_q = platform.strip().lower()
    region_q = region.strip().lower()

    matches: list[dict] = []
    for mid, post in LFG_POSTS.items():
        if post.get("guild_id") != gid or not post.get("active", True):
            continue
        if game_q and game_q not in post.get("game", ""):
            continue
        if platform_q and platform_q not in post.get("platform", ""):
            continue
        if region_q and region_q not in post.get("region", ""):
            continue
        if host and post.get("host_id") != host.id:
            continue
        matches.append(post)

    matches.sort(key=lambda p: p.get("created_at", 0), reverse=True)

    if not matches:
        await interaction.response.send_message("No active LFG posts match those filters right now.", ephemeral=True)
        return

    lines = []
    for post in matches[:10]:
        host_member = interaction.guild.get_member(post["host_id"])
        host_name = host_member.mention if host_member else f"<@{post['host_id']}>"
        lines.append(
            f"• **{post.get('game', 'unknown').title()}** | {host_name} | "
            f"need **{post.get('players_needed', '?')}** | [Open Post]({post.get('jump_url', '')})"
        )

    embed = discord.Embed(title="🎯 Active LFG Matches", description="\n".join(lines), color=0x3498DB)
    embed.set_footer(text=f"Showing {min(len(matches), 10)} of {len(matches)} active posts")
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


@client.tree.command(name="prestige", description="View and manage your prestige level (Phase 3 retention)", guild=GUILD_ID)
async def prestige(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    profile = _prestige_profile(target.id)
    prestige_lvl = profile.get("prestige", 0)
    resets = profile.get("resets", 0)
    gid = interaction.guild.id
    xp = XP_DATA.get(gid, {}).get(target.id, 0)
    level = _xp_to_level(xp)

    embed = discord.Embed(title=f"⭐ Prestige Profile — {target.display_name}", color=0xFFD700)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Prestige Level", value=f"**{prestige_lvl}/{PRESTIGE_LEVELS}**", inline=True)
    embed.add_field(name="Total Resets", value=str(resets), inline=True)
    embed.add_field(name="XP Multiplier", value=f"**{_xp_multiplier(target.id):.0%}**", inline=True)
    embed.add_field(name="Current Level", value=str(level), inline=True)
    embed.add_field(
        name="How to Level Prestige",
        value=(
            f"Use `/prestigereset` to reset your level to 1 and gain +1 Prestige.\n"
            f"Cost: **{PRESTIGE_RESET_COST}** PokeCoins (from casino)\n"
            f"Benefit: **{PRESTIGE_BONUS_XP_PER_LEVEL:.0%}** XP boost per prestige level"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="prestigereset", description="Reset your level to gain prestige (costs PokeCoins)", guild=GUILD_ID)
async def prestigereset(interaction: discord.Interaction):
    from pokemon_game import WALLETS, _wallet, STARTER_COINS
    
    uid = interaction.user.id
    profile = _prestige_profile(uid)
    prestige_lvl = profile.get("prestige", 0)

    if prestige_lvl >= PRESTIGE_LEVELS:
        await interaction.response.send_message("You've reached max prestige!", ephemeral=True)
        return

    coins = _wallet(uid)
    if coins < PRESTIGE_RESET_COST:
        await interaction.response.send_message(
            f"❌ Not enough PokeCoins. You have **{coins}**, need **{PRESTIGE_RESET_COST}**.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Deduct coins
    WALLETS[uid] = coins - PRESTIGE_RESET_COST

    # Increment prestige
    profile["prestige"] = min(prestige_lvl + 1, PRESTIGE_LEVELS)
    profile["resets"] = profile.get("resets", 0) + 1
    _save_prestige_data()

    # Reset guild XP to starter level
    gid = interaction.guild.id
    XP_DATA.setdefault(gid, {})[uid] = 100  # slight head-start

    new_prestige = profile.get("prestige", 0)
    multiplier = _xp_multiplier(uid)

    embed = discord.Embed(title="⭐ Prestige Achieved!", color=0xFFD700)
    embed.description = (
        f"You have reached **Prestige {new_prestige}/{PRESTIGE_LEVELS}**!\n\n"
        f"• Paid: **{PRESTIGE_RESET_COST}** PokeCoins\n"
        f"• Level reset to **1**\n"
        f"• New XP Multiplier: **{multiplier:.0%}**"
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@client.tree.command(name="cosmetics", description="View and purchase cosmetic upgrades", guild=GUILD_ID)
@app_commands.choices(action=[
    app_commands.Choice(name="Inventory", value="inventory"),
    app_commands.Choice(name="Shop", value="shop"),
])
async def cosmetics(interaction: discord.Interaction, action: str):
    from pokemon_game import WALLETS, _wallet
    
    uid = interaction.user.id

    if action == "inventory":
        owned = PLAYER_COSMETICS.get(uid, set())
        if not owned:
            await interaction.response.send_message(
                "You don't own any cosmetics yet. Use `/cosmetics shop` to browse.",
                ephemeral=True,
            )
            return
        lines = []
        for cos_id in owned:
            cos = COSMETICS.get(cos_id)
            if cos:
                lines.append(f"• **{cos['name']}** — {cos['desc']}")
        embed = discord.Embed(title="💎 Your Cosmetics", description="\n".join(lines), color=0x9B59B6)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif action == "shop":
        coins = _wallet(uid)
        lines = []
        for cos_id, cos in COSMETICS.items():
            affordable = "✅" if coins >= cos["cost"] else "❌"
            owned = "🔒" if cos_id in PLAYER_COSMETICS.get(uid, set()) else ""
            lines.append(
                f"{affordable} **{cos['name']}** — {cos['desc']}\n"
                f"   Cost: **{cos['cost']}** PokeCoins {owned}"
            )
        embed = discord.Embed(title="💎 Cosmetics Shop", color=0x9B59B6)
        embed.description = "\n".join(lines)
        embed.add_field(name="Your Balance", value=f"**{coins}** PokeCoins", inline=False)
        embed.add_field(
            name="How to Buy",
            value="Use `/cosmeticsbuy <name>` to purchase.\nExample: `/cosmeticsbuy title_badge`",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="cosmeticsbuy", description="Purchase a cosmetic upgrade", guild=GUILD_ID)
@app_commands.describe(cosmetic_id="The cosmetic ID (e.g., title_badge, border_glow)")
async def cosmeticsbuy(interaction: discord.Interaction, cosmetic_id: str):
    from pokemon_game import WALLETS, _wallet
    
    uid = interaction.user.id
    cos_id = cosmetic_id.lower().strip()

    if cos_id not in COSMETICS:
        await interaction.response.send_message(
            f"❌ Unknown cosmetic `{cos_id}`. Use `/cosmetics shop` to see available items.",
            ephemeral=True,
        )
        return

    cos = COSMETICS[cos_id]
    coins = _wallet(uid)

    if cos_id in PLAYER_COSMETICS.get(uid, set()):
        await interaction.response.send_message("You already own this cosmetic!", ephemeral=True)
        return

    if coins < cos["cost"]:
        await interaction.response.send_message(
            f"❌ Not enough PokeCoins. You have **{coins}**, need **{cos['cost']}**.",
            ephemeral=True,
        )
        return

    WALLETS[uid] = coins - cos["cost"]
    PLAYER_COSMETICS.setdefault(uid, set()).add(cos_id)
    _save_cosmetics_inventory()

    embed = discord.Embed(title="💎 Purchase Successful!", color=0x2ECC71)
    embed.description = f"You now own **{cos['name']}**!\n{cos['desc']}"
    embed.add_field(name="Spent", value=f"**{cos['cost']}** PokeCoins", inline=True)
    embed.add_field(name="Remaining", value=f"**{WALLETS[uid]}** PokeCoins", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

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

# ── Ticket Admin Commands ─────────────────────────────────────────────────────
@client.tree.command(name="ticketlist", description="List all currently open tickets (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def ticketlist(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    if not OPEN_TICKETS:
        await interaction.response.send_message("No tickets are currently open.", ephemeral=True)
        return
    lines = []
    for uid, cid in OPEN_TICKETS.items():
        member = interaction.guild.get_member(uid)
        ch = interaction.guild.get_channel(cid)
        name = member.mention if member else f"`{uid}`"
        chan = ch.mention if ch else f"`#{cid}` *(deleted?)*"
        lines.append(f"Ticket {name} -> {chan}")
    embed = discord.Embed(
        title=f"Open Tickets ({len(OPEN_TICKETS)})",
        description="\n".join(lines),
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="closeticket", description="Force-close a ticket channel (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="The ticket channel to close", reason="Reason for closing")
async def closeticket(interaction: discord.Interaction, channel: discord.TextChannel, reason: str = "Closed by admin"):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    log_ch = _resolve_ticket_log_channel(interaction.guild)
    if log_ch is None:
        print(f"[Tickets] WARNING: #{TICKET_LOG_NAME} channel not found — transcript not saved for {channel.name}")
    else:
        try:
            msgs = [m async for m in channel.history(limit=200, oldest_first=True)]
            transcript = "\n".join(
                f"[{m.created_at.strftime('%H:%M:%S')}] {m.author}: {m.content}"
                for m in msgs if not m.author.bot or m.content
            )
            embed = discord.Embed(title=f"Ticket Force-Closed - #{channel.name}", color=0xE74C3C)
            embed.description = f"```\n{transcript[:3900]}\n```" if transcript else "*No messages.*"
            embed.add_field(name="Closed by", value=interaction.user.mention)
            embed.add_field(name="Reason", value=reason)
            embed.timestamp = discord.utils.utcnow()
            await log_ch.send(embed=embed)
        except Exception as e:
            print(f"[Tickets] ERROR logging transcript for {channel.name}: {e}")
    for uid, cid in list(OPEN_TICKETS.items()):
        if cid == channel.id:
            del OPEN_TICKETS[uid]
            break
    TICKET_SLA_LAST_REMINDER.pop(channel.id, None)
    channel_name = channel.name
    await channel.delete(reason=f"Force closed by {interaction.user}: {reason}")
    await interaction.followup.send(f"Ticket #{channel_name} closed.", ephemeral=True)
    await _log_admin_cmd(interaction, "closeticket", f"#{channel_name} - {reason}")


@client.tree.command(name="addticketstaff", description="Give a role access to all ticket channels (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role="The staff role to grant ticket access")
async def addticketstaff(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cat = discord.utils.get(interaction.guild.categories, name=TICKET_CATEGORY_NAME)
    if not cat:
        await interaction.followup.send(f"No {TICKET_CATEGORY_NAME} category found. Run /setupticketchannel first.", ephemeral=True)
        return
    await cat.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True)
    updated = 0
    for ch in cat.text_channels:
        await ch.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True)
        updated += 1
    await interaction.followup.send(
        f"{role.mention} can now see all ticket channels ({updated} existing channels updated).", ephemeral=True
    )
    await _log_admin_cmd(interaction, "addticketstaff", f"{role.name} granted ticket access")


@client.tree.command(name="nukechannel", description="Delete all messages in a channel (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel to nuke (defaults to current channel)", amount="Max messages to delete (default: 100, max: 1000)")
async def nukechannel(interaction: discord.Interaction, channel: discord.TextChannel = None, amount: int = 100):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    target = channel or interaction.channel
    amount = max(1, min(amount, 1000))
    await interaction.response.defer(ephemeral=True)
    deleted = await target.purge(limit=amount)
    await interaction.followup.send(f"Nuked {len(deleted)} messages from {target.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "nukechannel", f"{len(deleted)} messages deleted in {target.mention}")


@client.tree.command(name="setupchat", description="Post a styled embed in a channel - rules, welcome, info (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    channel="Channel to post the embed in",
    title="Embed title",
    description="Embed body text (use \\n for new lines)",
    color="Hex color e.g. 5865F2 (optional, defaults to blue)",
    image_url="Optional image URL to attach to the embed",
)
async def setupchat(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    color: str = "5865F2",
    image_url: str = None,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    try:
        col = int(color.lstrip("#"), 16)
    except ValueError:
        col = 0x5865F2
    embed = discord.Embed(
        title=title,
        description=description.replace("\\n", "\n"),
        color=col,
    )
    embed.set_footer(text=f"Posted by {interaction.user.display_name}")
    if image_url:
        embed.set_image(url=image_url)
    await channel.send(embed=embed)
    await interaction.response.send_message(f"Embed posted in {channel.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "setupchat", f"Embed posted in {channel.mention}: \"{title}\"")


@client.tree.command(name="movechannel", description="Move a channel to a different category (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel to move", category="Exact name of the destination category")
async def cmd_movechannel(interaction: discord.Interaction, channel: discord.TextChannel, category: str):
    await movechannel(interaction, channel, category)

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


@client.tree.command(name="serverhealth", description="Show bot/server health diagnostics (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def serverhealth(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return

    guild = interaction.guild
    now = discord.utils.utcnow()
    uptime = _format_uptime(int((now - BOT_BOOT_TIME_UTC).total_seconds()))

    mod_log = _resolve_mod_log_channel(guild)
    ticket_log = _resolve_ticket_log_channel(guild)
    casino_ch = _resolve_or_track_text_channel(guild, "casino_channel", GAMBLING_CHANNEL_NAME, "casino-floor", "casino")
    pokemon_ch = _resolve_or_track_text_channel(guild, "pokemon_channel", POKEMON_CHANNEL_NAME, "pokemon-battle", "pokemon")
    music_ch = _resolve_or_track_text_channel(guild, "music_channel", MUSIC_CHANNEL_NAME, "music-channel", "music")
    verify_ch = _resolve_or_track_text_channel(guild, "verify_channel", VERIFY_CHANNEL_NAME, "verify", "✅-verify", "-verify")
    free_games_ch = _resolve_or_track_text_channel(guild, "free_games_channel", FREE_GAMES_CHANNEL_NAME, "freegames", "free-games")

    tracked_ticket_channels = [cid for cid in OPEN_TICKETS.values() if isinstance(guild.get_channel(cid), discord.TextChannel)]
    stale_tickets = 0
    for cid in tracked_ticket_channels:
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.TextChannel):
            age_hours = (now - ch.created_at).total_seconds() / 3600.0
            if age_hours >= TICKET_SLA_HOURS:
                stale_tickets += 1

    task_lines = [
        f"giveaway_check: {'✅' if giveaway_check.is_running() else '❌'}",
        f"streamer_check: {'✅' if streamer_check.is_running() else '❌'}",
        f"free_games_check: {'✅' if free_games_check.is_running() else '❌'}",
        f"empty_vc_cleanup: {'✅' if empty_vc_cleanup.is_running() else '❌'}",
        f"ticket_sla_check: {'✅' if ticket_sla_check.is_running() else '❌'}",
    ]

    embed = discord.Embed(title="🩺 Server Health", color=0x2ECC71)
    embed.add_field(name="Bot Runtime", value=f"Uptime: **{uptime}**\nMarker: `{STARTUP_MARKER}`\nPID: `{os.getpid()}`", inline=False)
    embed.add_field(
        name="Channel Wiring",
        value=(
            f"mod-log: {'✅' if mod_log else '❌'}\n"
            f"ticket-log: {'✅' if ticket_log else '❌'}\n"
            f"casino: {'✅' if casino_ch else '❌'}\n"
            f"pokemon: {'✅' if pokemon_ch else '❌'}\n"
            f"music: {'✅' if music_ch else '❌'}\n"
            f"verify: {'✅' if verify_ch else '❌'}\n"
            f"free-games: {'✅' if free_games_ch else '❌'}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Tickets",
        value=(
            f"Tracked open: **{len(tracked_ticket_channels)}**\n"
            f"SLA threshold: **{TICKET_SLA_HOURS}h**\n"
            f"Stale over SLA: **{stale_tickets}**"
        ),
        inline=True,
    )
    embed.add_field(name="Background Tasks", value="\n".join(task_lines), inline=False)
    embed.add_field(name="Data", value=f"Managed channel IDs: **{len(MANAGED_CHANNEL_IDS.get(str(guild.id), {}))}**\nPosted free-game IDs: **{len(POSTED_FREE_GAMES)}**", inline=False)
    embed.timestamp = now
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="weeklyrecap", description="Show a weekly community recap", guild=GUILD_ID)
async def weeklyrecap(interaction: discord.Interaction):
    guild = interaction.guild
    gid = guild.id
    now = discord.utils.utcnow()
    week_ago = now - datetime.timedelta(days=7)

    xp_map = XP_DATA.get(gid, {})
    voice_map = VOICE_MINUTES.get(gid, {})

    top_xp = sorted(xp_map.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_voice = sorted(voice_map.items(), key=lambda kv: kv[1], reverse=True)[:5]

    xp_lines = []
    for idx, (uid, xp) in enumerate(top_xp, 1):
        member = guild.get_member(uid)
        name = member.mention if member else f"User `{uid}`"
        xp_lines.append(f"`{idx}.` {name} — **{xp} XP**")
    if not xp_lines:
        xp_lines = ["No XP activity yet."]

    voice_lines = []
    for idx, (uid, mins) in enumerate(top_voice, 1):
        member = guild.get_member(uid)
        name = member.mention if member else f"User `{uid}`"
        voice_lines.append(f"`{idx}.` {name} — **{mins} min**")
    if not voice_lines:
        voice_lines = ["No voice activity yet."]

    new_members = [m for m in guild.members if m.joined_at and m.joined_at >= week_ago]
    active_giveaways = sum(1 for g in GIVEAWAYS.values() if not g.get("ended", False))
    open_tickets = sum(1 for cid in OPEN_TICKETS.values() if isinstance(guild.get_channel(cid), discord.TextChannel))
    followed_streamers = len(STREAMERS)

    embed = discord.Embed(title="📊 Weekly Community Recap", color=0x5865F2)
    embed.description = (
        f"Here is the latest pulse for **{guild.name}**.\n"
        f"New members (7d): **{len(new_members)}**\n"
        f"Open tickets: **{open_tickets}** • Active giveaways: **{active_giveaways}** • Followed streamers: **{followed_streamers}**"
    )
    embed.add_field(name="🏆 Top XP", value="\n".join(xp_lines), inline=False)
    embed.add_field(name="🎙️ Top Voice Time", value="\n".join(voice_lines), inline=False)
    embed.set_footer(text="Tip: run /serverhealth for live diagnostics")
    embed.timestamp = now
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="bot", description="About this bot and what it can do (Admin/Mod only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def bot_info(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.moderate_members):
        await interaction.response.send_message("🔒 This command is restricted to Admins and Moderators.", ephemeral=True)
        return
    
    # Page 1: Music & Gaming
    embed1 = discord.Embed(
        title="🤖 About This Bot — Page 1/4",
        description="A fully-featured gaming community bot. Use ▶️ to see more.",
        color=0x5865F2,
    )
    embed1.set_thumbnail(url=interaction.guild.me.display_avatar.url)
    embed1.add_field(name="🎵 Music Player", value=(
        "Play YouTube audio directly in voice channels.\n"
        "Search with autocomplete, queue songs, control volume, skip, pause & more.\n"
        "Use commands in **#music-channel**."
    ), inline=False)
    embed1.add_field(name="🎮 Gaming Tools", value=(
        "Post LFG ads, save gamertags, and unlock hidden game channels via button roles.\n"
        "Use `/setupgames` to create channels for all 15 games."
    ), inline=False)
    
    # Page 2: Community & Economy
    embed2 = discord.Embed(
        title="🤖 About This Bot — Page 2/4",
        description="Community features and progression systems.",
        color=0x5865F2,
    )
    embed2.set_thumbnail(url=interaction.guild.me.display_avatar.url)
    embed2.add_field(name="📊 Levels & XP", value=(
        "Members earn XP every minute of chatting or being in voice.\n"
        "Level ups are announced in-channel. Use `/rank` and `/leaderboard`."
    ), inline=False)
    embed2.add_field(name="🎉 Giveaways", value=(
        "Admins run timed giveaways with `/giveaway`. Members enter by reacting 🎉.\n"
        "Winners are picked randomly when the timer ends."
    ), inline=False)
    
    # Page 3: Support & Alerts
    embed3 = discord.Embed(
        title="🤖 About This Bot — Page 3/4",
        description="Support and notification systems.",
        color=0x5865F2,
    )
    embed3.set_thumbnail(url=interaction.guild.me.display_avatar.url)
    embed3.add_field(name="🎫 Support Tickets", value=(
        "Members open private support channels via a button panel.\n"
        "Transcripts are saved to #ticket-logs when closed."
    ), inline=False)
    embed3.add_field(name="📡 Streamer Alerts", value=(
        "Follow Twitch streamers and get notified in **#streamer-alerts** when they go live.\n"
        "Manage with `/addstreamer`, `/removestreamer`, `/streamers`."
    ), inline=False)
    
    # Page 4: Admin & Moderation
    embed4 = discord.Embed(
        title="🤖 About This Bot — Page 4/4",
        description="Administrative and safety features.",
        color=0x5865F2,
    )
    embed4.set_thumbnail(url=interaction.guild.me.display_avatar.url)
    embed4.add_field(name="🏷️ Reaction Roles & Free Games", value=(
        "🏷️ Admins add reaction roles to messages with `/reactionrole`.\n"
        "🎮 Auto-posts 100% off Steam games to **#free-games** every 4 hours."
    ), inline=False)
    embed4.add_field(name="📨 Tracking & Safety", value=(
        "Invite tracking showing who invited each member.\n"
        "🛡️ Auto-moderation, word filter, link quarantine, and comprehensive logging."
    ), inline=False)
    embed4.set_footer(text="Use /help for user commands • Use /adminhelp for mod commands")
    
    embeds = [embed1, embed2, embed3, embed4]
    view = PaginatedHelpView(embeds, interaction.user.id)
    await interaction.response.send_message(embed=embeds[0], view=view, ephemeral=True)

@client.tree.command(name="help", description="Show all available bot commands (Admin/Mod only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def help_command(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.moderate_members):
        await interaction.response.send_message("🔒 This command is restricted to Admins and Moderators.", ephemeral=True)
        return
    
    # Page 1: Getting Started
    embed1 = discord.Embed(
        title="📖 Bot Commands — Page 1/5 (Getting Started)",
        description="**New here?** Try `/quickstart` first!",
        color=0x5865F2
    )
    embed1.add_field(name="🆕 Getting Started", value=(
        "`/quickstart` — 30-second new-player guide (NEW PLAYERS START HERE)\n"
        "`/setupverify` — Rebuild the verification button channel (admin)\n"
        "`/gamertag` — Save your gaming profile\n"
        "`/bot` — About this bot (admin/mod only)"
    ), inline=False)
    embed1.add_field(name="ℹ️ Navigation", value="Use ◀️ ▶️ to flip through pages", inline=False)
    
    # Page 2: Progression & Economy
    embed2 = discord.Embed(
        title="📖 Bot Commands — Page 2/5 (Progression & Economy)",
        color=0x5865F2
    )
    embed2.add_field(name="📊 Progression & Economy", value=(
        "`/rank [user]` — View level, XP, voice time\n"
        "`/leaderboard` — Top 10 XP members\n"
        "`/prestige [user]` — View prestige level & XP multiplier\n"
        "`/prestigereset` — Reset to gain prestige (costs PokeCoins)\n"
        "`/cosmetics inventory|shop` — View/browse cosmetics\n"
        "`/cosmeticsbuy <id>` — Purchase cosmetic"
    ), inline=False)
    
    # Page 3: Gaming & Social
    embed3 = discord.Embed(
        title="📖 Bot Commands — Page 3/5 (Gaming & Social)",
        color=0x5865F2
    )
    embed3.add_field(name="🎮 Gaming & Social", value=(
        "`/lfg <game> <players> [desc]` — Post LFG with RSVP buttons\n"
        "`/lfgfilter [game] [platform] [region]` — Find active LFG posts\n"
        "`/gamertags [user]` — View someone's gamertags\n"
        "`/invites [user]` — Check invite count\n"
        "`/personalspace` — Create dynamic voice rooms (admin only)"
    ), inline=False)
    
    # Page 4: Music
    embed4 = discord.Embed(
        title="📖 Bot Commands — Page 4/5 (Music)",
        color=0x5865F2
    )
    embed4.add_field(name="🎵 Music (in #music-channel)", value=(
        "`/play <query>` — Search & play song\n"
        "`/pause` — Pause playback\n"
        "`/resume` — Resume playback\n"
        "`/skip` — Skip current song\n"
        "`/stop` — Stop & clear queue\n"
        "`/leave` — Disconnect bot\n"
        "`/queue` — View song queue\n"
        "`/volume <0-100>` — Set volume"
    ), inline=False)
    
    # Page 5: Community & Admin
    embed5 = discord.Embed(
        title="📖 Bot Commands — Page 5/5 (Community & Admin)",
        color=0x5865F2
    )
    embed5.add_field(name="📈 Community & Info", value=(
        "`/weeklyrecap` — Weekly community stats"
    ), inline=False)
    embed5.add_field(name="🔧 Admin/Mod Commands", value=(
        "Use `/adminhelp` to see moderation, setup, and safety commands.\n"
        "⭐ **Tip:** Only admins and moderators can view help commands!"
    ), inline=False)
    embed5.set_footer(text="Page 5/5 • Use /adminhelp for detailed mod commands")
    
    embeds = [embed1, embed2, embed3, embed4, embed5]
    view = PaginatedHelpView(embeds, interaction.user.id)
    await interaction.response.send_message(embed=embeds[0], view=view, ephemeral=True)


@client.tree.command(name="quickstart", description="New player guide — Get started in 30 seconds", guild=GUILD_ID)
async def quickstart(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🚀 Welcome to the Gaming Zone!",
        description="Follow these 4 quick steps to get started.",
        color=0x2ECC71
    )

    embed.add_field(name="Step 1️⃣: Verify", value=(
        f"Go to **#{VERIFY_CHANNEL_NAME}** and click **Verify — Get Access**.\n"
        "This unlocks your member channels and game features."
    ), inline=False)

    embed.add_field(name="Step 2️⃣: Save Your Gamertag", value=(
        "Run `/gamertag <platform> <username>`\n"
        "Example: `/gamertag ps5 MyPlayName`"
    ), inline=False)

    embed.add_field(name="Step 3️⃣: Find a Squad", value=(
        "Post an LFG: `/lfg valorant 3 looking for comp`\n"
        "Browse posts: `/lfgfilter game:valorant`\n"
        "Join using the **Going** button"
    ), inline=False)

    embed.add_field(name="Step 4️⃣: Earn XP & Level Up", value=(
        "💬 Chat = 15 XP/min (with cooldown)\n"
        "🎙️ Voice = 1 XP/min\n"
        "View your rank: `/rank`"
    ), inline=False)

    embed.add_field(name="💡 Pro Tips", value=(
        f"• If verify is missing, ask an admin to run `/setupverify`\n"
        "• Use `/prestige` to reset and gain multipliers\n"
        "• Buy cosmetics with PokeCoins at `/cosmetics shop`\n"
        "• Need help? Type `/help`"
    ), inline=False)

    embed.set_footer(text="Questions? Ask a mod in #support")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="adminhelp", description="Admin/Mod command guide (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def adminhelp(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.moderate_members):
        await interaction.response.send_message("🔒 Moderator permission required.", ephemeral=True)
        return

    # Page 1: Moderation & Utilities
    embed1 = discord.Embed(
        title="🔧 Admin & Moderation — Page 1/3",
        description="Moderator command reference",
        color=0xFF6B6B
    )
    embed1.add_field(name="🔨 Core Moderation", value=(
        "`/ban <user> [reason]` — Ban a member\n"
        "`/unban <user_id> [reason]` — Unban by ID\n"
        "`/kick <user> [reason]` — Kick a member\n"
        "`/mute <user>` — Mute indefinitely\n"
        "`/unmute <user>` — Remove mute\n"
        "`/timeout <user> <hours> <minutes> [reason]` — Timeout a member\n"
        "`/banlist` — View all bans\n"
        "`/clear <amount>` — Delete messages"
    ), inline=False)
    
    # Page 2: Safety & Setup
    embed2 = discord.Embed(
        title="🔧 Admin & Moderation — Page 2/3",
        description="Safety automation and community setup",
        color=0xFF6B6B
    )
    embed2.add_field(name="👮 Mod Utilities", value=(
        "`/whitelist <user>` — Protect from mod actions\n"
        "`/unwhitelist <user>` — Remove protection\n"
        "`/announce <channel> <message>` — Post announcement"
    ), inline=False)
    embed2.add_field(name="🔒 Safety Automation (Phase 4)", value=(
        "`/raidmode <enable|disable>` — Lock server (mods only)\n"
        "`/accountage <days> [enabled]` — Block new accounts from links\n"
        "`/linkquarantine <view|clear> [user_id]` — Manage link records"
    ), inline=False)
    
    # Page 3: Channels & Community
    embed3 = discord.Embed(
        title="🔧 Admin & Moderation — Page 3/3",
        description="Channel setup and community features",
        color=0xFF6B6B
    )
    embed3.add_field(name="🎫 Tickets & Roles", value=(
        "`/setupticketchannel <channel>` — Post ticket panel\n"
        "`/reactionrole <message_id> <emoji> <role>` — Bind role to reaction\n"
        "`/setupgames` — Create game channels & buttons\n"
        "`/personalspace [category]` — Create dynamic voice rooms"
    ), inline=False)
    embed3.add_field(name="📡 Community & Events", value=(
        "`/addstreamer <username> <platform>` — Follow streamer\n"
        "`/removestreamer <username>` — Unfollow streamer\n"
        "`/setupfreegames` — Post free Steam games\n"
        "`/giveaway <prize> <minutes> [winners]` — Start giveaway\n"
        "`/endgiveaway <message_id>` — End early\n"
        "`/serverhealth` — View bot diagnostics"
    ), inline=False)
    embed3.set_footer(text="Page 3/3 • Check the wiki for additional admin guides")
    
    embeds = [embed1, embed2, embed3]
    view = PaginatedHelpView(embeds, interaction.user.id)
    await interaction.response.send_message(embed=embeds[0], view=view, ephemeral=True)


@client.tree.command(name="setupfreegames", description="Create #free-games and post current Steam deals now (Admin only)", guild=GUILD_ID)
@app_commands.default_permissions(administrator=True)
async def setupfreegames(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    ch = _resolve_or_track_text_channel(guild, "free_games_channel", FREE_GAMES_CHANNEL_NAME, "freegames", "free-games")
    if not ch:
        ch = await guild.create_text_channel(
            FREE_GAMES_CHANNEL_NAME,
            topic="🎮 Free games on Steam — auto-updated every 4 hours",
            reason="Free games setup",
        )
        _remember_channel(guild, "free_games_channel", ch)
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
    else:
        _remember_channel(guild, "free_games_channel", ch)

    await interaction.followup.send(f"✅ Fetching current free games and posting to {ch.mention}…", ephemeral=True)

    loop = asyncio.get_running_loop()
    games = await loop.run_in_executor(None, _fetch_steam_free_games)
    announced_urls = await _recent_announced_free_game_urls(ch)

    games_to_post = [g for g in games if g.get("url", "").strip() not in announced_urls]

    for game in games_to_post:
        POSTED_FREE_GAMES.add(game["id"])
    _save_posted_games()
    await _post_free_games(ch, games_to_post)

    await interaction.followup.send(
        f"{'✅ Posted **' + str(len(games_to_post)) + '** new free game(s) with images and claim buttons.' if games_to_post else '⚠️ No new free games found right now.'} The channel auto-updates every 4 hours.",
        ephemeral=True,
    )
    await _log_admin_cmd(interaction, "setupfreegames", f"Channel: {ch.mention} | Games posted: {len(games_to_post)}")


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
