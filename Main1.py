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
import base64
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

    async def _purge_stale_global_casino_commands(self) -> None:
        """Remove legacy global casino commands so only the primary guild copy remains."""
        app_id = self.application_id or (self.user.id if self.user else None)
        if app_id is None:
            return

        casino_names = {
            "casinomenu",
            "setupcasino",
            "daily",
            "work",
            "givepokcoin",
            "slots",
            "blackjack",
            "coinflip",
            "roulette",
            "dice",
            "highlow",
            "plinko",
            "heist",
        }

        try:
            global_commands = await self.http.get_global_commands(app_id)
        except Exception as e:
            print(f"[Sync] Could not fetch global commands for cleanup: {e}")
            return

        removed = 0
        for command in global_commands:
            if command.get("name") not in casino_names:
                continue
            cmd_id = command.get("id")
            if not cmd_id:
                continue
            try:
                await self.http.delete_global_command(app_id, int(cmd_id))
                removed += 1
            except Exception as e:
                print(f"[Sync] Could not delete stale global command {command.get('name')}: {e}")

        if removed:
            print(f"[Sync] Removed {removed} stale global casino command(s).")

    async def _purge_all_global_commands(self) -> None:
        """Remove all global app commands to avoid duplicate global+guild command entries."""
        app_id = self.application_id or (self.user.id if self.user else None)
        if app_id is None:
            return

        try:
            global_commands = await self.http.get_global_commands(app_id)
        except Exception as e:
            print(f"[Sync] Could not fetch global commands for full cleanup: {e}")
            return

        removed = 0
        for command in global_commands:
            cmd_id = command.get("id")
            if not cmd_id:
                continue
            try:
                await self.http.delete_global_command(app_id, int(cmd_id))
                removed += 1
            except Exception as e:
                print(f"[Sync] Could not delete global command {command.get('name')}: {e}")

        if removed:
            print(f"[Sync] Removed {removed} global command(s) to prevent duplicates.")

    async def _purge_stale_guild_casino_commands(self, guild_id: int) -> None:
        """Remove legacy guild casino commands before syncing a clean command tree."""
        app_id = self.application_id or (self.user.id if self.user else None)
        if app_id is None:
            return

        casino_names = {
            "casinomenu",
            "setupcasino",
            "daily",
            "work",
            "givepokcoin",
            "slots",
            "blackjack",
            "coinflip",
            "roulette",
            "dice",
            "highlow",
            "plinko",
            "heist",
        }

        try:
            guild_commands = await self.http.get_guild_commands(app_id, guild_id)
        except Exception as e:
            print(f"[Sync] Could not fetch guild commands for cleanup ({guild_id}): {e}")
            return

        removed = 0
        for command in guild_commands:
            if command.get("name") not in casino_names:
                continue
            cmd_id = command.get("id")
            if not cmd_id:
                continue
            try:
                await self.http.delete_guild_command(app_id, guild_id, int(cmd_id))
                removed += 1
            except Exception as e:
                print(f"[Sync] Could not delete stale guild command {command.get('name')} in {guild_id}: {e}")

        if removed:
            print(f"[Sync] Removed {removed} stale guild casino command(s) in {guild_id}.")

    async def _hard_reset_guild_commands(self, guild_id: int) -> None:
        """Force-clear remote guild command cache so sync starts from a clean slate."""
        app_id = self.application_id or (self.user.id if self.user else None)
        if app_id is None:
            return
        try:
            await self.http.bulk_upsert_guild_commands(app_id, guild_id, [])
            print(f"[Sync] Hard-reset guild commands for {guild_id}.")
        except Exception as e:
            print(f"[Sync] Hard-reset failed for guild {guild_id}: {e}")

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
        inviter = None
        used_invite = None
        try:
            new_invites = await member.guild.invites()
            old_cache = INVITE_CACHE.get(member.guild.id, {})
            for inv in new_invites:
                old = old_cache.get(inv.code)
                old_uses = old.uses if old else None
                new_uses = inv.uses
                if old_uses is not None and new_uses is not None and new_uses > old_uses:
                    used_invite = inv
                    inviter = inv.inviter
                    if inviter:
                        INVITE_COUNTS.setdefault(member.guild.id, {})
                        new_count = INVITE_COUNTS[member.guild.id].get(inviter.id, 0) + 1
                        INVITE_COUNTS[member.guild.id][inviter.id] = new_count
                        # Award XP for successful invite
                        try:
                            old_lvl, new_lvl, leveled_up = _add_xp(member.guild.id, inviter.id, 100)
                            level_ch = _resolve_levels_channel(member.guild)
                            gain_target = level_ch
                            total_xp = XP_DATA.get(member.guild.id, {}).get(inviter.id, 0)
                            current_level = _xp_to_level(total_xp)
                            next_level = current_level + 1
                            xp_for_next = _xp_required(next_level)
                            xp_this_level = total_xp - sum(_xp_required(l) for l in range(current_level))
                            progress = min(1.0, xp_this_level / xp_for_next) if xp_for_next else 0.0
                            bar = "▰" * int(progress * 18) + "▱" * (18 - int(progress * 18))
                            msg = (
                                f"🎉 {inviter.mention} earned +100 XP for inviting a new member!\n"
                                f"Progress to Level {next_level}\n{bar} {xp_this_level}/{xp_for_next} XP"
                            )
                            if gain_target:
                                await gain_target.send(msg)
                        except Exception:
                            pass
                    break
            INVITE_CACHE[member.guild.id] = {inv.code: inv for inv in new_invites}
        except discord.Forbidden:
            print(f"[Invite] Missing Manage Guild/Invites permission in {member.guild.name} ({member.guild.id})")
        except Exception as e:
            print(f"[Invite] Could not resolve inviter for join in {member.guild.name} ({member.guild.id}): {e}")

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
                inviter_display = getattr(inviter, "mention", str(inviter))
                embed.set_footer(text=f"Invited by {inviter_display} Â· Joined {member.guild.name}")
            elif used_invite:
                embed.set_footer(text=f"Joined via invite code {used_invite.code} Â· {member.guild.name}")
            else:
                embed.set_footer(text=f"Joined {member.guild.name}")
            embed.timestamp = discord.utils.utcnow()
            await welcome_ch.send(embed=embed)

        # ── Social alert in the configured social alerts channel ──────────
        alert_ch = _resolve_social_alert_channel(member.guild)
        if alert_ch:
            desc = f"📥 {member.mention} **joined the server** — member #{member.guild.member_count}"
            if inviter:
                inviter_display = getattr(inviter, "mention", str(inviter))
                desc += f"\n🔗 Invited by **{inviter_display}**"
            elif used_invite:
                desc += f"\n🔗 Invite used: `{used_invite.code}`"
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

        # ── Bot voice reconnect ───────────────────────────────────────────────
        # If the BOT itself got disconnected while a song was playing, try to
        # reconnect to the same channel and resume from where we left off.
        if uid == self.user.id and before.channel is not None and after.channel is None:
            state = get_music_state(gid)
            song = state.current
            if song:
                print(f"[Voice] Bot was disconnected from voice during playback — attempting reconnect to #{before.channel.name}")
                await asyncio.sleep(2)  # brief pause before reconnect
                try:
                    new_vc = await before.channel.connect(self_deaf=True)
                    state.voice_client = new_vc
                    # Re-queue the interrupted song at the front and restart
                    state.queue.appendleft(song)
                    state.current = None
                    loop = asyncio.get_running_loop()
                    await play_next(gid, loop)
                    await _post_music_panel(gid, force_new=True)
                    print(f"[Voice] Reconnected and resumed: {song.title}")
                except Exception as e:
                    print(f"[Voice] Reconnect failed: {e}")
                    state.current = None

        # ── Personal Space logic ──────────────────────────────────────────────
        lobby_id = PERSONAL_SPACE_LOBBY.get(gid)

        # User joined the lobby trigger channel → create their private VC
        if lobby_id and after.channel and after.channel.id == lobby_id:
            guild = member.guild
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
                _save_levels_data()
                # Award XP for voice chat (e.g., 10 XP per minute, minimum 1 minute)
                if minutes > 0:
                    xp_gain = minutes * 10
                    old_lvl, new_lvl, leveled_up = _add_xp(gid, uid, xp_gain)
                    total_xp = XP_DATA.get(gid, {}).get(uid, 0)
                    current_level = _xp_to_level(total_xp)
                    next_level = current_level + 1
                    xp_for_next = _xp_required(next_level)
                    xp_this_level = total_xp - sum(_xp_required(l) for l in range(current_level))
                    progress = min(1.0, xp_this_level / xp_for_next) if xp_for_next else 0.0
                    bar = "▰" * int(progress * 18) + "▱" * (18 - int(progress * 18))
                    if leveled_up:
                        try:
                            level_embed = _build_gz_levelup_card(
                                member.guild,
                                member,
                                new_lvl,
                                total_xp,
                                xp_this_level,
                                xp_for_next,
                            )
                            level_embed.add_field(name="Voice XP Bonus", value=f"+{xp_gain} XP", inline=True)
                            level_embed.set_footer(text="Gaming Zone Level Card • Auto-removes soon")
                            announce_ch = _resolve_levels_channel(member.guild) or member.guild.system_channel
                            if announce_ch:
                                await announce_ch.send(
                                    content=member.mention,
                                    embed=level_embed,
                                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                                    delete_after=12,
                                )
                        except Exception:
                            pass

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
        if message.channel.id == MOD_REVIEW_CHANNEL_ID:
            return

        review_ch = message.guild.get_channel(MOD_REVIEW_CHANNEL_ID)
        if not isinstance(review_ch, discord.TextChannel):
            review_ch = _resolve_mod_log_channel(message.guild)
        if not review_ch:
            return

        actor, deleted_count = await self._find_recent_delete_actor(
            message.guild,
            channel_id=message.channel.id,
            target_id=message.author.id,
        )

        content = (message.content or "").strip()
        attachment_urls = [a.url for a in message.attachments if getattr(a, "url", None)]

        embed = discord.Embed(title="🗑️ Deleted Message Review", color=0xE67E22)
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
        embed.add_field(name="Content", value=(content[:1024] if content else "*No text content*"), inline=False)
        if attachment_urls:
            preview = "\n".join(attachment_urls[:5])
            if len(attachment_urls) > 5:
                preview += f"\n...and {len(attachment_urls) - 5} more"
            embed.add_field(name="Attachments", value=preview[:1024], inline=False)
        if actor:
            actor_value = f"{actor.mention} (`{actor.id}`)"
            if deleted_count:
                actor_value += f"\nAudit count: `{deleted_count}`"
            embed.add_field(name="Deleted By", value=actor_value, inline=False)
        embed.set_footer(text=f"Author ID: {message.author.id} | Message ID: {message.id}")
        embed.timestamp = discord.utils.utcnow()

        view = DeletedMessageReviewView(
            author_id=message.author.id,
            channel_id=message.channel.id,
            content=content,
            attachment_urls=attachment_urls,
        )
        await review_ch.send(embed=embed, view=view)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Capture deletes even when the message was not cached by the gateway."""
        if payload.guild_id is None:
            return
        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return
        review_ch = guild.get_channel(MOD_REVIEW_CHANNEL_ID)
        if not isinstance(review_ch, discord.TextChannel):
            review_ch = _resolve_mod_log_channel(guild)
        if review_ch is None:
            return

        channel_mention = f"<#{payload.channel_id}>"
        cached = payload.cached_message
        if cached is not None:
            # Cached deletes are already handled by on_message_delete.
            return

        actor, deleted_count = await self._find_recent_delete_actor(
            guild,
            channel_id=payload.channel_id,
        )

        embed = discord.Embed(title="🗑️ Deleted Message Review", color=0xE67E22)
        embed.add_field(name="Channel", value=channel_mention, inline=False)
        embed.add_field(name="Message ID", value=f"`{payload.message_id}`", inline=True)
        embed.add_field(name="Content", value="*Unavailable (message was not cached)*", inline=False)
        if actor:
            actor_value = f"{actor.mention} (`{actor.id}`)"
            if deleted_count:
                actor_value += f"\nAudit count: `{deleted_count}`"
            embed.add_field(name="Deleted By", value=actor_value, inline=False)
        embed.add_field(name="Review Status", value="Needs manual review (cannot restore uncached content)", inline=False)
        embed.timestamp = discord.utils.utcnow()
        await review_ch.send(embed=embed)

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
            client.add_view(IdleRPGStarterView())
            client.add_view(IdleRPGStatusPanelView())
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
                    restored_count, missing_count = _restore_xp_snapshot_to_guild(primary_guild)
                    if restored_count or missing_count:
                        print(f"[Levels] Snapshot restore applied: restored={restored_count}, missing={missing_count}")
                except Exception as e:
                    print(f"[Levels] Snapshot restore failed: {e}")
                try:
                    await _apply_requested_channel_repairs(primary_guild)
                except Exception as e:
                    print(f"[Repair] Channel repair failed in {primary_guild.name}: {e}")
                try:
                    await _ensure_idlerpg_channel_access(primary_guild)
                except Exception as e:
                    print(f"[IdleRPG] Channel access repair failed in {primary_guild.name}: {e}")
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

            # Keep existing command registrations; avoid destructive command purges.

            # Do NOT auto-create channels on startup/restart.
            # Only validate and warn in the primary guild; admins can run setup commands explicitly.
            marker_guild = primary_guild
            marker_ch = None
            role_ch = None
            ticket_ch = None
            if marker_guild is not None:
                # Log channels are only provisioned on explicit /setupchannels command, not auto-reconciled at startup.
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
                print("[Startup] âš ï¸  No primary guild resolved; startup marker not sent.")
            elif marker_ch is None:
                print(
                    f"[Startup] âš ï¸  No bot-logs channel found in primary guild "
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

            # Sync slash commands globally (can take longer to fully propagate).
            try:
                synced_global = await self.tree.sync()
                print(f"[Sync] Synced {len(synced_global)} global slash commands")
                try:
                    synced_names = ", ".join(sorted(cmd.name for cmd in synced_global))
                    print(f"[Sync] Global commands: {synced_names}")
                except Exception:
                    pass

                # Also sync to primary guild for immediate availability while global propagation catches up.
                if primary_guild is not None:
                    guild_obj = discord.Object(id=primary_guild.id)
                    synced_guild = await self.tree.sync(guild=guild_obj)
                    print(f"[Sync] Synced {len(synced_guild)} guild commands for immediate availability in {primary_guild.id}")
            except Exception as e:
                print(f"[Sync] Could not sync commands: {e}")
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
LEVELS_CHANNEL_ID = 1496681573762863116
LOGS_CATEGORY_ID = 1496416640022220902
TICKET_LOG_CHANNEL_ID = 1496371222223655103
BOT_LOG_CHANNEL_ID = 1498287670005071884
IDLERPG_CHAT_CHANNEL_ID = 1505097749245202482
MOD_REVIEW_CHANNEL_ID = 1498240928480104568
TICKET_LOG_CATEGORY_ID = LOGS_CATEGORY_ID
TICKET_LOG_NAME = "\U0001F3AB\u2503ticket-logs"
STREAMER_PARENT_ID = 711335159189864469
SOCIAL_ALERTS_CHANNEL_NAME = "📣┃social-alerts"
LEGACY_SOCIAL_ALERTS_CHANNEL_NAME = "social-alerts"
MOD_LOG_NAME  = "📋┃mod-logs"   # admin-only mod action log
ROLE_LOG_NAME = "📜┃role-logs"  # admin-only role assignment log
BOT_LOG_NAME  = "🤖┃bot-logs"   # private bot runtime/economy event log
GAMBLING_CHANNEL_NAME = "casino-floor"   # dedicated casino text channel
POKEMON_CHANNEL_NAME  = "pokemon-battle" # dedicated pokemon battle channel
GAMER_ROLE_NAME       = "Gamer"          # role granted on verification — unlocks feature channels
VERIFY_CHANNEL_NAME   = "✅-verify"       # visible to unverified; hidden once Gamer role is granted
VERIFY_REWARD_COINS   = 1800              # one-time reward when a member verifies
MUSIC_CATEGORY_NAME   = "â™¦â”ƒð™ð™šð™­ð™© ð˜¾ð™ð™–ð™£ð™£ð™šð™¡ð™¨â”ƒâ™¦"  # category where music-channel is created
WHITELIST: set[int] = set()  # stores whitelisted user IDs
VERIFY_EMBED_MARKER = "GZ_VERIFY_EMBED_V1"
_MANAGED_CHANNELS_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "managed_channels.json")
MANAGED_CHANNEL_IDS: dict[str, dict[str, int]] = {}
STARTUP_MARKER = f"boot-{int(time.time())}-{os.getpid()}"

# IdleRPG data (kept separate from server XP/rank/economy systems)
_IDLERPG_BASE_PATH = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
_IDLERPG_SAVE = os.path.join(_IDLERPG_BASE_PATH, "idlerpg_data.json")
IDLERPG_DATA: dict[str, dict] = {}

IDLERPG_GODS = [
    {"name": "Kord", "emoji": "⚡", "desc": "War god. Higher XP from adventures.", "class": "Warrior", "bonus": "xp"},
    {"name": "Mystra", "emoji": "🔮", "desc": "Arcane goddess. Better odds for rare finds.", "class": "Mage", "bonus": "rare"},
    {"name": "Mask", "emoji": "🕶️", "desc": "Shadow god. Higher coin rewards.", "class": "Rogue", "bonus": "coins"},
    {"name": "Lathander", "emoji": "☀️", "desc": "Dawn god. Higher favor gains.", "class": "Cleric", "bonus": "favor"},
    {"name": "Mielikki", "emoji": "🌿", "desc": "Forest goddess. Better odds for extra loot.", "class": "Ranger", "bonus": "loot"},
    {"name": "Bahamut", "emoji": "🐉", "desc": "Dragon god. Balanced bonus to all rewards.", "class": "Paladin", "bonus": "balanced"},
]

IDLERPG_RACES = [
    {"name": "Human", "emoji": "🧑", "desc": "Balanced and adaptable."},
    {"name": "Elf", "emoji": "🧝", "desc": "Agile and lucky."},
    {"name": "Dwarf", "emoji": "🛡️", "desc": "Tough and resilient."},
    {"name": "Orc", "emoji": "🪓", "desc": "Brutal strength and battle focus."},
    {"name": "Tiefling", "emoji": "😈", "desc": "Hellborn will and dark charisma."},
    {"name": "Dragonborn", "emoji": "🐲", "desc": "Scaled honor and draconic power."},
    {"name": "Halfling", "emoji": "🦶", "desc": "Small frame, huge luck."},
    {"name": "Gnome", "emoji": "⚙️", "desc": "Inventive mind and arcane curiosity."},
    {"name": "Aasimar", "emoji": "😇", "desc": "Celestial-blooded with radiant resolve."},
    {"name": "Goblin", "emoji": "🧨", "desc": "Scrappy survivor with chaotic tactics."},
    {"name": "Tabaxi", "emoji": "🐈", "desc": "Feline hunter with fast reflexes."},
    {"name": "Genasi", "emoji": "🌪️", "desc": "Element-touched wanderer."},
]

IDLERPG_CLASSES = [
    {"name": "Warrior", "emoji": "⚔️", "desc": "Frontline brawler with solid power."},
    {"name": "Mage", "emoji": "🪄", "desc": "Arcane caster with high burst."},
    {"name": "Rogue", "emoji": "🗡️", "desc": "Fast and deadly opportunist."},
    {"name": "Cleric", "emoji": "✨", "desc": "Holy support with strong sustain."},
    {"name": "Ranger", "emoji": "🏹", "desc": "Ranged hunter with survival skills."},
    {"name": "Paladin", "emoji": "🛡", "desc": "Sacred knight with balanced might."},
]

IDLERPG_ALIGNMENTS = [
    {"name": "Lawful Good", "emoji": "🟦", "desc": "Honorable and compassionate defender."},
    {"name": "Neutral Good", "emoji": "🟩", "desc": "Kind-hearted and practical helper."},
    {"name": "Chaotic Good", "emoji": "🟢", "desc": "Freedom-driven hero against tyranny."},
    {"name": "Lawful Neutral", "emoji": "⚖️", "desc": "Duty first, emotion second."},
    {"name": "True Neutral", "emoji": "🧭", "desc": "Balance above all extremes."},
    {"name": "Chaotic Neutral", "emoji": "🟠", "desc": "Unpredictable and fiercely independent."},
    {"name": "Lawful Evil", "emoji": "🔷", "desc": "Disciplined ambition at any cost."},
    {"name": "Neutral Evil", "emoji": "🟣", "desc": "Self-serving and opportunistic."},
    {"name": "Chaotic Evil", "emoji": "🔺", "desc": "Destructive, ruthless, and volatile."},
]

IDLERPG_CLASS_CRATE_COST = 325
IDLERPG_CLASS_GEAR_POOLS = {
    "Warrior": [("Colossus Greatsword", "⚔️", 155), ("Ironquake Axe", "🪓", 150), ("Bulwark Plate", "🛡️", 148)],
    "Mage": [("Astral Focus Staff", "🪄", 162), ("Void Codex", "📘", 150), ("Runebound Orb", "🔮", 154)],
    "Rogue": [("Nightfang Daggers", "🗡️", 158), ("Silent Hood", "🕶️", 146), ("Shadowstep Boots", "👢", 144)],
    "Cleric": [("Sanctified Mace", "🔨", 152), ("Dawnlit Censer", "✨", 147), ("Aegis Reliquary", "🛡️", 149)],
    "Ranger": [("Windsplit Bow", "🏹", 156), ("Tracker Cloak", "🧥", 145), ("Falcon Quiver", "🧷", 148)],
    "Paladin": [("Oathkeeper Blade", "⚔️", 160), ("Dragoncrest Shield", "🛡️", 152), ("Radiant Helm", "⛑️", 150)],
}

IDLERPG_GEAR_POOL = [
    ("Neon Blade", "⚔️", 90),
    ("Aegis Core", "🛡️", 80),
    ("Quantum Staff", "🪄", 110),
    ("Pulse Dagger", "🗡️", 95),
    ("Ember Halberd", "🔥", 118),
    ("Frostbound Pike", "❄️", 116),
    ("Thunder Maul", "⚡", 122),
    ("Rune Carbine", "🔫", 114),
    ("Starforged Buckler", "🛡️", 108),
    ("Wyrmfang Spear", "🪓", 120),
]

IDLERPG_LOOT_POOL = [
    ("Ancient Relic", "🔮", 0, 25),
    ("Solar Shard", "☀️", 0, 20),
    ("Moon Sigil", "🌙", 0, 18),
    ("Storm Totem", "⛈️", 0, 22),
    ("Whispering Idol", "🗿", 0, 21),
    ("Dragon Scale Fragment", "🐉", 0, 23),
    ("Sunken Crown Gem", "💠", 0, 19),
    ("Runic Prism", "🔷", 0, 24),
    ("Blood Oath Token", "🩸", 0, 20),
    ("Verdant Fetish", "🌿", 0, 18),
    ("Abyssal Sigil", "🕳️", 0, 22),
    ("Clockwork Core", "⚙️", 0, 19),
    ("Celestial Feather", "🪽", 0, 21),
    ("Eldritch Eye", "👁️", 0, 24),
    ("Phoenix Ember", "🪶", 0, 23),
    ("Leviathan Fang", "🦈", 0, 22),
    ("Chrono Dust", "⏳", 0, 20),
    ("Void Lantern", "🏮", 0, 21),
    ("Stormheart Crystal", "💎", 0, 23),
    ("Obsidian Idol", "🗿", 0, 22),
    ("Dawn Oath Scroll", "📜", 0, 19),
    ("Fae Bloom", "🌸", 0, 18),
]

IDLERPG_ENCOUNTER_TABLE = [
    {
        "encounter": "Roadside Skirmish",
        "emoji": "⚡",
        "mult": 1.0,
        "duration": (120, 240),
        "flavors": [
            "You followed old tracks into a moonlit pass where bandits waited in ambush.",
            "A village elder begged for aid, and your party marched into a goblin raid.",
            "Mercenaries blocked the trade road and challenged your escort for passage.",
        ],
    },
    {
        "encounter": "Ruin Sweep",
        "emoji": "🧭",
        "mult": 1.12,
        "duration": (180, 300),
        "flavors": [
            "You combed shattered battlements for relic caches beneath crumbling stone.",
            "A collapsed temple opened hidden tunnels guarded by animated statues.",
            "Dusty catacombs revealed sealed chambers and forgotten ward circles.",
        ],
    },
    {
        "encounter": "Dungeon Delve",
        "emoji": "🔥",
        "mult": 1.35,
        "duration": (300, 480),
        "flavors": [
            "You descended into a cursed crypt guarded by elite sentinels.",
            "Deep beneath ruined halls, a warded vault challenged your resolve.",
            "A labyrinth of traps and sigils forced your team into brutal close combat.",
        ],
    },
    {
        "encounter": "Forbidden Library",
        "emoji": "📚",
        "mult": 1.45,
        "duration": (330, 520),
        "flavors": [
            "Sentient tomes screamed incantations as you hunted the archive's core seal.",
            "Candlelit stacks shifted like a maze while cursed librarians pursued intruders.",
            "You bargained with a spectral curator for access to a locked grimmoire vault.",
        ],
    },
    {
        "encounter": "Boss Event",
        "emoji": "👑",
        "mult": 1.8,
        "duration": (480, 720),
        "flavors": [
            "A dragon-kin tyrant rose from an ancient throne and marked your name.",
            "The dungeon heart awakened, and a legendary warlord challenged your oath.",
            "A fallen champion returned in cursed armor, demanding trial by steel.",
        ],
    },
]


def _idlerpg_god_for_class(class_name: str | None) -> dict | None:
    if not class_name:
        return None
    return next((g for g in IDLERPG_GODS if g.get("class", "").lower() == class_name.lower()), None)


def _idlerpg_make_class_gear(class_name: str, profile: dict | None = None) -> dict:
    pool = IDLERPG_CLASS_GEAR_POOLS.get(class_name) or IDLERPG_CLASS_GEAR_POOLS.get("Warrior")
    recent_drops = _idlerpg_recent_values(profile or {}, "recent_drops")
    name, emoji, value = _idlerpg_pick_non_repeat(pool, recent_drops)
    rarity_label = "Legendary" if value >= 158 else "Epic" if value >= 148 else "Rare"
    item = {
        "id": _idlerpg_next_item_id(),
        "name": name,
        "emoji": emoji,
        "type": "gear",
        "value": value,
        "favor": 0,
        "class_tag": class_name,
        "rarity": rarity_label,
    }
    if profile is not None:
        _idlerpg_remember_value(profile, "recent_drops", item["name"])
    return item


def _idlerpg_recent_values(profile: dict, key: str) -> list[str]:
    values = profile.get(key)
    if isinstance(values, list):
        return [str(v) for v in values if v]
    return []


def _idlerpg_remember_value(profile: dict, key: str, value: str, limit: int = 4) -> None:
    history = _idlerpg_recent_values(profile, key)
    history.append(value)
    profile[key] = history[-max(1, limit):]


def _idlerpg_pick_non_repeat(pool: list[tuple], recent_names: list[str]) -> tuple:
    if not pool:
        return tuple()
    fresh = [entry for entry in pool if str(entry[0]) not in recent_names]
    return random.choice(fresh or pool)


class DeletedMessageReviewView(discord.ui.View):
    def __init__(self, author_id: int, channel_id: int, content: str, attachment_urls: list[str]):
        super().__init__(timeout=3600)
        self.author_id = author_id
        self.channel_id = channel_id
        self.content = content
        self.attachment_urls = attachment_urls

    @staticmethod
    def _can_moderate(member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.administrator or perms.manage_messages or perms.moderate_members

    def _disable_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="Approve (Restore)", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not self._can_moderate(interaction.user):
            await interaction.response.send_message("You need message moderation permissions to approve.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Original channel was not found, so this message cannot be restored.", ephemeral=True)
            return

        author = interaction.guild.get_member(self.author_id) if interaction.guild else None
        author_ref = author.mention if author else f"<@{self.author_id}>"
        restored_body = self.content or "*No text content*"

        if self.attachment_urls:
            attachment_block = "\n".join(self.attachment_urls)
            restored_body = f"{restored_body}\n\nAttachments:\n{attachment_block}"

        await channel.send(
            f"**[Restored by {interaction.user.mention}]** {author_ref}: {restored_body}"
        )

        self._disable_buttons()
        await interaction.message.edit(view=self)
        await interaction.response.send_message("Message approved and restored.", ephemeral=True)

    @discord.ui.button(label="Deny (Keep Deleted + Warn)", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not self._can_moderate(interaction.user):
            await interaction.response.send_message("You need message moderation permissions to deny.", ephemeral=True)
            return

        if interaction.guild:
            author = interaction.guild.get_member(self.author_id)
            if author:
                try:
                    await author.send("Your deleted message was denied by moderation and will not be restored.")
                except Exception:
                    pass

        self._disable_buttons()
        await interaction.message.edit(view=self)
        await interaction.response.send_message("Message denied. It remains deleted.", ephemeral=True)


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
        if key == "streamer_channel" and ("stream" in topic and "auto-mod" in topic):
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


def _resolve_streamer_parent(guild: discord.Guild) -> discord.CategoryChannel | None:
    target = guild.get_channel(STREAMER_PARENT_ID)
    if isinstance(target, discord.CategoryChannel):
        return target
    if isinstance(target, discord.TextChannel):
        return target.category
    return None


def _resolve_streamer_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return _resolve_or_track_text_channel(
        guild,
        "streamer_channel",
        STREAMER_CHANNEL_NAME,
        "streamer-alerts",
        "streamer-channel",
        "streamers",
    )


async def _apply_requested_channel_repairs(guild: discord.Guild) -> None:
    streamer_topic = "Post Twitch, YouTube, or Kick stream links here without triggering auto-mod advertising penalties."
    streamer_parent = _resolve_streamer_parent(guild)
    streamer_ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
    }

    ticket_ch = guild.get_channel(TICKET_LOG_CHANNEL_ID)
    if isinstance(ticket_ch, discord.TextChannel) and ticket_ch.name != TICKET_LOG_NAME:
        await ticket_ch.edit(name=TICKET_LOG_NAME)
        _remember_channel(guild, "ticket_log", ticket_ch)
        print(f"[Repair] Renamed ticket log to #{TICKET_LOG_NAME} in {guild.name}")

    streamer_ch = _resolve_streamer_channel(guild)
    if streamer_ch is None:
        await guild.create_text_channel(
            STREAMER_CHANNEL_NAME,
            overwrites=streamer_ow,
            topic=streamer_topic,
            category=streamer_parent,
        )
        print(f"[Repair] Created #{STREAMER_CHANNEL_NAME} in {guild.name}")
        return

    edit_kwargs = {"overwrites": streamer_ow, "topic": streamer_topic}
    if streamer_ch.name != STREAMER_CHANNEL_NAME:
        edit_kwargs["name"] = STREAMER_CHANNEL_NAME
    if streamer_parent and streamer_ch.category != streamer_parent:
        edit_kwargs["category"] = streamer_parent
    if len(edit_kwargs) > 2:
        await streamer_ch.edit(**edit_kwargs)
        print(f"[Repair] Updated #{STREAMER_CHANNEL_NAME} in {guild.name}")


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
    """Create feature channels when explicitly requested via /setupchannels (casino, pokemon, music, streamer, verify)."""
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
        (POKEMON_CHANNEL_NAME,  "âš”ï¸ Challenge others to Pokemon battles here! /pokemon battle", None, ["pokemon-battle", "pokemon"]),
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

    streamer_ow = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True
        ),
    }
    streamer_topic = "Post Twitch, YouTube, or Kick stream links here without triggering auto-mod advertising penalties."
    streamer_parent = _resolve_streamer_parent(guild)
    streamer_ch = _resolve_streamer_channel(guild)
    if streamer_ch:
        edit_kwargs = {"overwrites": streamer_ow, "topic": streamer_topic}
        if streamer_parent and streamer_ch.category != streamer_parent:
            edit_kwargs["category"] = streamer_parent
        await streamer_ch.edit(**edit_kwargs)
        _remember_channel(guild, "streamer_channel", streamer_ch)
    else:
        streamer_ch = await guild.create_text_channel(
            STREAMER_CHANNEL_NAME,
            overwrites=streamer_ow,
            topic=streamer_topic,
            category=streamer_parent,
        )
        _remember_channel(guild, "streamer_channel", streamer_ch)
        print(f"[Channels] Created #{STREAMER_CHANNEL_NAME} in {guild.name}")

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


async def _ensure_idlerpg_channel_access(guild: discord.Guild) -> discord.TextChannel | None:
    """Apply Gamer-only access to the configured IdleRPG channel ID only."""
    gamer_role = discord.utils.get(guild.roles, name=GAMER_ROLE_NAME)
    if not gamer_role:
        gamer_role = await guild.create_role(
            name=GAMER_ROLE_NAME,
            colour=discord.Colour.green(),
            reason="Auto-created: grants access to IdleRPG chat after verification",
        )
        print(f"[IdleRPG] Created @{GAMER_ROLE_NAME} role in {guild.name}")

    idlerpg_ch = guild.get_channel(IDLERPG_CHAT_CHANNEL_ID)
    if not isinstance(idlerpg_ch, discord.TextChannel):
        print(f"[IdleRPG] Channel id {IDLERPG_CHAT_CHANNEL_ID} not found in {guild.name}")
        return None

    restricted_ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        gamer_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True
        ),
    }

    # Keep third-party bots (like IdleRPG) usable in this channel.
    for member in guild.members:
        if member.bot:
            restricted_ow[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                embed_links=True,
            )

    await idlerpg_ch.edit(overwrites=restricted_ow)
    _remember_channel(guild, "idlerpg_channel", idlerpg_ch)
    return idlerpg_ch

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
            f"âš”ï¸ **#{POKEMON_CHANNEL_NAME}** — Pokemon battles\n"
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
_LEVELS_SAVE = os.path.join(_IDLERPG_BASE_PATH, "levels_data.json")
_LEVELS_RESTORE_STATE_SAVE = os.path.join(_IDLERPG_BASE_PATH, "levels_restore_state.json")
XP_GAIN_FEED_ENABLED = False

XP_RESTORE_SNAPSHOT_ID = "gz-may19-2026-levels-restore-v1"
XP_RESTORE_SNAPSHOT: list[tuple[str, int]] = [
    ("Kobe The Machine AKA the mf GOAT", 9343),
    ("CenturioxXx", 5738),
    ("Bukimi the restless", 2974),
    ("Faded_Nova", 2906),
    ("MusicBot", 2754),
    ("ArticFox", 1943),
    ("Berrie the Platypus", 1442),
    ("DaddyMaxson", 1411),
    ("NordicVortex", 1394),
    ("Chair Force Tofu", 1370),
    ("Novobatzky", 898),
    ("Pontius Pilate", 870),
    ("GotHMommy", 293),
    ("velendia", 139),
    ("No More Heroes", 93),
]

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
    _save_levels_data()
    old_lvl = _xp_to_level(before)
    new_lvl = _xp_to_level(guild_xp[user_id])
    return old_lvl, new_lvl, new_lvl > old_lvl


def _load_levels_data() -> None:
    global XP_DATA, VOICE_MINUTES
    if not os.path.exists(_LEVELS_SAVE):
        return
    try:
        with open(_LEVELS_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            xp_raw = data.get("xp", {})
            voice_raw = data.get("voice", {})
            if isinstance(xp_raw, dict):
                XP_DATA = {
                    int(gid): {int(uid): int(val) for uid, val in members.items() if str(uid).isdigit()}
                    for gid, members in xp_raw.items()
                    if isinstance(members, dict) and str(gid).isdigit()
                }
            if isinstance(voice_raw, dict):
                VOICE_MINUTES = {
                    int(gid): {int(uid): int(val) for uid, val in members.items() if str(uid).isdigit()}
                    for gid, members in voice_raw.items()
                    if isinstance(members, dict) and str(gid).isdigit()
                }
    except Exception as e:
        print(f"[Levels] Warning: could not load level data — {e}")


def _save_levels_data() -> None:
    try:
        payload = {
            "xp": XP_DATA,
            "voice": VOICE_MINUTES,
        }
        with open(_LEVELS_SAVE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"[Levels] Warning: could not save level data — {e}")


def _normalize_member_name(value: str) -> str:
    return "".join(ch for ch in str(value).strip().lower() if ch.isalnum())


def _load_levels_restore_state() -> dict:
    if not os.path.exists(_LEVELS_RESTORE_STATE_SAVE):
        return {}
    try:
        with open(_LEVELS_RESTORE_STATE_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[Levels] Warning: could not load restore state — {e}")
        return {}


def _save_levels_restore_state(data: dict) -> None:
    try:
        with open(_LEVELS_RESTORE_STATE_SAVE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[Levels] Warning: could not save restore state — {e}")


def _restore_xp_snapshot_to_guild(guild: discord.Guild, force: bool = False) -> tuple[int, int]:
    state = _load_levels_restore_state()
    applied = set(state.get("applied_snapshots", [])) if isinstance(state.get("applied_snapshots"), list) else set()
    if XP_RESTORE_SNAPSHOT_ID in applied and not force:
        return (0, 0)

    gid = guild.id
    guild_xp = XP_DATA.setdefault(gid, {})

    by_name: dict[str, discord.Member] = {}
    for member in guild.members:
        keys = {
            _normalize_member_name(member.display_name),
            _normalize_member_name(member.name),
            _normalize_member_name(getattr(member, "global_name", "") or ""),
        }
        for key in keys:
            if key and key not in by_name:
                by_name[key] = member

    restored = 0
    missing = 0
    for raw_name, xp_value in XP_RESTORE_SNAPSHOT:
        member = by_name.get(_normalize_member_name(raw_name))
        if not member:
            missing += 1
            continue
        # Keep the higher value if someone already earned more.
        guild_xp[member.id] = max(int(guild_xp.get(member.id, 0)), int(xp_value))
        restored += 1

    _save_levels_data()
    applied.add(XP_RESTORE_SNAPSHOT_ID)
    state["applied_snapshots"] = sorted(applied)
    _save_levels_restore_state(state)
    return restored, missing


def _resolve_levels_channel(guild: discord.Guild) -> discord.TextChannel | None:
    ch = guild.get_channel(LEVELS_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        _remember_channel(guild, "levels_channel", ch)
        return ch
    # Fallback names if configured ID was renamed/mismatched.
    return _resolve_or_track_text_channel(guild, "levels_channel", "levels-chat", "levels", "level-ups", "level-up")


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
TICKET_LOG_NAME      = "\U0001F3AB\u2503ticket-logs"
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
STREAMER_CHANNEL_NAME = "🎬┃streamer-channel"
STREAM_LINK_PATTERNS: tuple[str, ...] = (
    r"(?:https?://)?(?:www\.)?twitch\.tv/\S+",
    r"(?:https?://)?(?:www\.)?kick\.com/\S+",
    r"(?:https?://)?(?:www\.)?youtube\.com/(?:watch\?v=\S+|live/\S+|@\S+)",
    r"(?:https?://)?youtu\.be/\S+",
)

# ── Free Games ────────────────────────────────────────────────────────────────
FREE_GAMES_CHANNEL_NAME = "free-games"
_FREE_GAMES_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posted_free_games.json")
POSTED_FREE_GAMES: set[int] = set()  # app IDs already announced
FREE_GAMES_FIRST_CYCLE = True


def _contains_stream_link(content: str) -> bool:
    return any(re.search(pattern, content, re.IGNORECASE) for pattern in STREAM_LINK_PATTERNS)

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
        f"â” Maybe: **{maybe}**\n"
        f"âŒ Pass: **{passing}**"
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


def _load_idlerpg_data() -> None:
    global IDLERPG_DATA
    if not os.path.exists(_IDLERPG_SAVE):
        return
    try:
        with open(_IDLERPG_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            IDLERPG_DATA = {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        print(f"[IdleRPG] Warning: could not load data — {e}")


def _save_idlerpg_data() -> None:
    try:
        with open(_IDLERPG_SAVE, "w", encoding="utf-8") as f:
            json.dump(IDLERPG_DATA, f)
    except Exception as e:
        print(f"[IdleRPG] Warning: could not save data — {e}")


def _idlerpg_next_item_id() -> int:
    return int(time.time() * 1000) + random.randint(100, 999)


def _idlerpg_profile(user: discord.abc.User, create: bool = False) -> dict | None:
    uid = str(user.id)
    profile = IDLERPG_DATA.get(uid)
    if profile is None and create:
        profile = {
            "name": user.display_name,
            "level": 1,
            "xp": 0,
            "money": 100,
            "inventory": [],
            "equipped": None,
            "god": None,
            "favor": 0,
            "race": None,
            "class": None,
            "alignment": None,
            "recent_drops": [],
            "recent_encounters": [],
        }
        IDLERPG_DATA[uid] = profile
        _save_idlerpg_data()
    return profile


def _idlerpg_level_up(profile: dict) -> tuple[int, int]:
    old_level = int(profile.get("level", 1))
    xp = int(profile.get("xp", 0))
    level = old_level
    while xp >= level * 120:
        xp -= level * 120
        level += 1
    profile["xp"] = xp
    profile["level"] = level
    return old_level, level


def _idlerpg_make_item(kind: str | None = None, profile: dict | None = None) -> dict:
    roll = random.random()
    recent_drops = _idlerpg_recent_values(profile or {}, "recent_drops")
    if kind == "gear":
        name, emoji, value = _idlerpg_pick_non_repeat(IDLERPG_GEAR_POOL, recent_drops)
        item = {
            "id": _idlerpg_next_item_id(),
            "name": name,
            "emoji": emoji,
            "type": "gear",
            "value": value,
            "favor": 0,
        }
        if profile is not None:
            _idlerpg_remember_value(profile, "recent_drops", item["name"])
        return item
    if kind == "super_chest":
        item = {
            "id": _idlerpg_next_item_id(),
            "name": "Mythic Arcane Chest",
            "emoji": "🪬",
            "type": "chest",
            "value": 0,
            "favor": 0,
            "tier": "mythic",
        }
        if profile is not None:
            _idlerpg_remember_value(profile, "recent_drops", item["name"])
        return item
    if kind == "chest":
        is_mythic = random.random() < 0.10
        item = {
            "id": _idlerpg_next_item_id(),
            "name": "Mythic Arcane Chest" if is_mythic else "Magic Chest",
            "emoji": "🪬" if is_mythic else "🧰",
            "type": "chest",
            "value": 0,
            "favor": 0,
            "tier": "mythic" if is_mythic else "magic",
        }
        if profile is not None:
            _idlerpg_remember_value(profile, "recent_drops", item["name"])
        return item
    if roll < 0.012:
        item = {
            "id": _idlerpg_next_item_id(),
            "name": "Mythic Arcane Chest",
            "emoji": "🪬",
            "type": "chest",
            "value": 0,
            "favor": 0,
            "tier": "mythic",
        }
        if profile is not None:
            _idlerpg_remember_value(profile, "recent_drops", item["name"])
        return item
    if roll < 0.08:
        item = {
            "id": _idlerpg_next_item_id(),
            "name": "Magic Chest",
            "emoji": "🧰",
            "type": "chest",
            "value": 0,
            "favor": 0,
            "tier": "magic",
        }
        if profile is not None:
            _idlerpg_remember_value(profile, "recent_drops", item["name"])
        return item
    if roll < 0.5:
        name, emoji, value = _idlerpg_pick_non_repeat(IDLERPG_GEAR_POOL, recent_drops)
        item = {
            "id": _idlerpg_next_item_id(),
            "name": name,
            "emoji": emoji,
            "type": "gear",
            "value": value,
            "favor": 0,
        }
        if profile is not None:
            _idlerpg_remember_value(profile, "recent_drops", item["name"])
        return item
    name, emoji, value, favor = _idlerpg_pick_non_repeat(IDLERPG_LOOT_POOL, recent_drops)
    item = {
        "id": _idlerpg_next_item_id(),
        "name": name,
        "emoji": emoji,
        "type": "loot",
        "value": value,
        "favor": favor,
    }
    if profile is not None:
        _idlerpg_remember_value(profile, "recent_drops", item["name"])
    return item


def _idlerpg_allowed_channel(interaction: discord.Interaction) -> tuple[bool, discord.TextChannel | None]:
    ch = interaction.guild.get_channel(IDLERPG_CHAT_CHANNEL_ID) if interaction.guild else None
    if not isinstance(ch, discord.TextChannel):
        return True, None
    if interaction.channel_id != IDLERPG_CHAT_CHANNEL_ID:
        return False, ch
    return True, ch


def _idlerpg_build_missing(profile: dict) -> list[str]:
    missing: list[str] = []
    if not profile.get("race"):
        missing.append("Race (/race)")
    if not profile.get("class"):
        missing.append("Class (/class)")
    if not profile.get("alignment"):
        missing.append("Alignment (/alignment)")
    if not profile.get("god"):
        missing.append("God (/follow)")
    return missing


def _idlerpg_find_item(profile: dict, item_id: int) -> dict | None:
    for item in profile.get("inventory", []):
        if int(item.get("id", -1)) == int(item_id):
            return item
    return None


def _idlerpg_sacrifice_gain(item: dict) -> int:
    item_type = str(item.get("type", "")).lower()
    if item_type == "loot":
        return int(item.get("favor", 10))
    if item_type == "gear":
        return max(5, int(item.get("value", 50)) // 8)
    if item_type == "chest":
        tier = str(item.get("tier", "magic")).lower()
        return 70 if tier == "mythic" else 35
    return 8


def _idlerpg_item_rarity(item: dict) -> tuple[str, int, str]:
    item_type = str(item.get("type", "item")).lower()

    if item_type == "chest":
        tier = str(item.get("tier", "magic")).lower()
        if tier == "mythic":
            return "Mythic", 0xF1C40F, "🪬"
        return "Rare", 0x9B59B6, "🧰"

    if item_type == "gear":
        value = int(item.get("value", 0))
        if value >= 158:
            return "Legendary", 0xF1C40F, "🟡"
        if value >= 148:
            return "Epic", 0x9B59B6, "🟣"
        if value >= 128:
            return "Rare", 0x3498DB, "🔵"
        return "Uncommon", 0x2ECC71, "🟢"

    if item_type == "loot":
        favor = int(item.get("favor", 0))
        if favor >= 24:
            return "Epic", 0x9B59B6, "🟣"
        if favor >= 20:
            return "Rare", 0x3498DB, "🔵"
        return "Uncommon", 0x2ECC71, "🟢"

    return "Common", 0x95A5A6, "⚪"


def _idlerpg_progress_bar(ratio: float, width: int = 10) -> str:
    ratio = max(0.0, min(1.0, ratio))
    fill = int(round(ratio * width))
    return "█" * fill + "░" * (width - fill)


def _idlerpg_prepare_adventure(profile: dict) -> dict:
    roll = random.random()
    if roll < 0.45:
        candidates = IDLERPG_ENCOUNTER_TABLE[:2]
    elif roll < 0.90:
        candidates = IDLERPG_ENCOUNTER_TABLE[1:4]
    else:
        candidates = IDLERPG_ENCOUNTER_TABLE[3:]

    recent_encounters = _idlerpg_recent_values(profile, "recent_encounters")
    fresh_candidates = [c for c in candidates if c.get("encounter") not in recent_encounters]
    selected = random.choice(fresh_candidates or candidates)

    encounter = str(selected.get("encounter", "Roadside Skirmish"))
    emoji = str(selected.get("emoji", "⚡"))
    mult = float(selected.get("mult", 1.0))
    d_min, d_max = selected.get("duration", (120, 240))
    duration = random.randint(int(d_min), int(d_max))
    flavor = random.choice(selected.get("flavors", ["You set out on another contract."]))
    _idlerpg_remember_value(profile, "recent_encounters", encounter)

    now_ts = int(time.time())
    return {
        "start_ts": now_ts,
        "end_ts": now_ts + duration,
        "encounter": encounter,
        "encounter_emoji": emoji,
        "mult": mult,
        "flavor": flavor,
        "base_xp": random.randint(14, 34),
        "base_coin": random.randint(25, 75),
        "base_favor": random.randint(2, 6),
        "item": _idlerpg_make_item(profile=profile),
    }


def _idlerpg_resolve_adventure(profile: dict) -> dict | None:
    adv = profile.get("active_adventure")
    if not isinstance(adv, dict):
        return None

    xp_gain = int(adv.get("base_xp", 0) * float(adv.get("mult", 1.0)))
    coin_gain = int(adv.get("base_coin", 0) * float(adv.get("mult", 1.0)))
    favor_gain = int(adv.get("base_favor", 0) * (1.2 if adv.get("encounter") == "Boss Event" else 1.0))

    god = (profile.get("god") or "").lower()
    god_def = next((g for g in IDLERPG_GODS if g.get("name", "").lower() == god), None)
    bonus = god_def.get("bonus") if isinstance(god_def, dict) else None
    if bonus == "xp":
        xp_gain = int(xp_gain * 1.30)
    elif bonus == "coins":
        coin_gain = int(coin_gain * 1.30)
    elif bonus == "favor":
        favor_gain = int(favor_gain * 1.40)
    elif bonus == "rare" and random.random() < 0.15:
        profile.setdefault("inventory", []).append(_idlerpg_make_item(kind="chest", profile=profile))
    elif bonus == "loot" and random.random() < 0.20:
        profile.setdefault("inventory", []).append(_idlerpg_make_item(profile=profile))
    elif bonus == "balanced":
        xp_gain = int(xp_gain * 1.10)
        coin_gain = int(coin_gain * 1.10)
        favor_gain = int(favor_gain * 1.10)

    if random.random() < 0.05:
        profile.setdefault("inventory", []).append(_idlerpg_make_item(kind="chest", profile=profile))
    if random.random() < 0.006:
        profile.setdefault("inventory", []).append(_idlerpg_make_item(kind="super_chest", profile=profile))

    item = adv.get("item") if isinstance(adv.get("item"), dict) else _idlerpg_make_item(profile=profile)
    profile.setdefault("inventory", []).append(item)
    profile["last_drop_id"] = int(item.get("id", -1))
    profile["xp"] = int(profile.get("xp", 0)) + xp_gain
    profile["money"] = int(profile.get("money", 0)) + coin_gain
    profile["favor"] = int(profile.get("favor", 0)) + favor_gain

    old_lvl, new_lvl = _idlerpg_level_up(profile)
    profile["active_adventure"] = None

    return {
        "encounter": adv.get("encounter", "Skirmish"),
        "encounter_emoji": adv.get("encounter_emoji", "⚡"),
        "flavor": adv.get("flavor", "Mission complete."),
        "xp": xp_gain,
        "coins": coin_gain,
        "favor": favor_gain,
        "item": item,
        "old_level": old_lvl,
        "new_level": new_lvl,
    }


class IdleRPGBuildView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    def _not_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id != self.user_id

    @discord.ui.select(
        placeholder="Choose your race",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Human", emoji="🧑", value="Human"),
            discord.SelectOption(label="Elf", emoji="🧝", value="Elf"),
            discord.SelectOption(label="Dwarf", emoji="🛡️", value="Dwarf"),
            discord.SelectOption(label="Orc", emoji="🪓", value="Orc"),
            discord.SelectOption(label="Tiefling", emoji="😈", value="Tiefling"),
            discord.SelectOption(label="Dragonborn", emoji="🐲", value="Dragonborn"),
            discord.SelectOption(label="Halfling", emoji="🦶", value="Halfling"),
            discord.SelectOption(label="Gnome", emoji="⚙️", value="Gnome"),
            discord.SelectOption(label="Aasimar", emoji="😇", value="Aasimar"),
            discord.SelectOption(label="Goblin", emoji="🧨", value="Goblin"),
            discord.SelectOption(label="Tabaxi", emoji="🐈", value="Tabaxi"),
            discord.SelectOption(label="Genasi", emoji="🌪️", value="Genasi"),
        ],
    )
    async def race_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if self._not_owner(interaction):
            await interaction.response.send_message("This build panel belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
            return
        profile["race"] = select.values[0]
        _save_idlerpg_data()
        await interaction.response.send_message(f"Race set to **{select.values[0]}**.", ephemeral=True)

    @discord.ui.select(
        placeholder="Choose your class",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Warrior", emoji="⚔️", value="Warrior"),
            discord.SelectOption(label="Mage", emoji="🪄", value="Mage"),
            discord.SelectOption(label="Rogue", emoji="🗡️", value="Rogue"),
            discord.SelectOption(label="Cleric", emoji="✨", value="Cleric"),
            discord.SelectOption(label="Ranger", emoji="🏹", value="Ranger"),
            discord.SelectOption(label="Paladin", emoji="🛡", value="Paladin"),
        ],
    )
    async def class_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if self._not_owner(interaction):
            await interaction.response.send_message("This build panel belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
            return
        profile["class"] = select.values[0]
        _save_idlerpg_data()
        await interaction.response.send_message(f"Class set to **{select.values[0]}**.", ephemeral=True)

    @discord.ui.select(
        placeholder="Choose your god",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Kord", emoji="⚡", value="Kord"),
            discord.SelectOption(label="Mystra", emoji="🔮", value="Mystra"),
            discord.SelectOption(label="Mask", emoji="🕶️", value="Mask"),
            discord.SelectOption(label="Lathander", emoji="☀️", value="Lathander"),
            discord.SelectOption(label="Mielikki", emoji="🌿", value="Mielikki"),
            discord.SelectOption(label="Bahamut", emoji="🐉", value="Bahamut"),
        ],
    )
    async def god_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if self._not_owner(interaction):
            await interaction.response.send_message("This build panel belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
            return
        selected_god = select.values[0]
        class_name = profile.get("class")
        expected = _idlerpg_god_for_class(class_name)
        if not class_name:
            await interaction.response.send_message("Choose your class first so your god can align with it.", ephemeral=True)
            return
        if not expected or expected.get("name") != selected_god:
            await interaction.response.send_message(
                f"Your class **{class_name}** aligns with **{expected['name'] if expected else 'no god'}**.",
                ephemeral=True,
            )
            return
        profile["god"] = selected_god
        _save_idlerpg_data()
        await interaction.response.send_message(f"God set to **{selected_god}**.", ephemeral=True)

    @discord.ui.select(
        placeholder="Choose your alignment",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Lawful Good", emoji="🟦", value="Lawful Good"),
            discord.SelectOption(label="Neutral Good", emoji="🟩", value="Neutral Good"),
            discord.SelectOption(label="Chaotic Good", emoji="🟢", value="Chaotic Good"),
            discord.SelectOption(label="Lawful Neutral", emoji="⚖️", value="Lawful Neutral"),
            discord.SelectOption(label="True Neutral", emoji="🧭", value="True Neutral"),
            discord.SelectOption(label="Chaotic Neutral", emoji="🟠", value="Chaotic Neutral"),
            discord.SelectOption(label="Lawful Evil", emoji="🔷", value="Lawful Evil"),
            discord.SelectOption(label="Neutral Evil", emoji="🟣", value="Neutral Evil"),
            discord.SelectOption(label="Chaotic Evil", emoji="🔺", value="Chaotic Evil"),
        ],
        row=3,
    )
    async def alignment_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if self._not_owner(interaction):
            await interaction.response.send_message("This build panel belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
            return
        profile["alignment"] = select.values[0]
        _save_idlerpg_data()
        await interaction.response.send_message(f"Alignment set to **{select.values[0]}**.", ephemeral=True)

    @discord.ui.button(label="Check Build", style=discord.ButtonStyle.primary, emoji="✅")
    async def check_build(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._not_owner(interaction):
            await interaction.response.send_message("This build panel belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
            return
        missing = _idlerpg_build_missing(profile)
        if missing:
            await interaction.response.send_message("Build incomplete: " + ", ".join(missing), ephemeral=True)
            return
        await interaction.response.send_message("Build complete. You can now start missions with the Adventure Hub.", ephemeral=True)


class IdleRPGAdventureView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=1800)
        self.user_id = user_id

    @discord.ui.button(label="Check Mission Timer", style=discord.ButtonStyle.primary, emoji="⏱️")
    async def check_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This mission panel belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        adv = profile.get("active_adventure") if profile else None
        if not isinstance(adv, dict):
            await interaction.response.send_message("No active mission. Use `/adventure` to deploy one.", ephemeral=True)
            return
        now_ts = int(time.time())
        end_ts = int(adv.get("end_ts", now_ts))
        if now_ts >= end_ts:
            await interaction.response.send_message("Mission is complete. Use `/status` to collect rewards.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Mission in progress. Ends <t:{end_ts}:R> (at <t:{end_ts}:t>). Use `/status` to check in.",
            ephemeral=True,
        )

    @discord.ui.button(label="What Next?", style=discord.ButtonStyle.secondary, emoji="🧭")
    async def what_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Flow: `/status` to check mission -> `/inventory` to manage loot -> `/adventure` for next run.",
            ephemeral=True,
        )

    @discord.ui.button(label="Start Adventure", style=discord.ButtonStyle.success, emoji="🚀")
    async def start_adventure(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This mission panel belongs to another player.", ephemeral=True)
            return
        try:
            await idlerpg_adventure.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Start Adventure button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Adventure button failed. Please use /adventure once and I will fix the panel.", ephemeral=True)
            else:
                await interaction.response.send_message("Adventure button failed. Please use /adventure once and I will fix the panel.", ephemeral=True)

    @discord.ui.button(label="Open Inventory", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def open_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This mission panel belongs to another player.", ephemeral=True)
            return
        try:
            await idlerpg_inventory.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Open Inventory button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Inventory button failed. Please use /inventory once and I will fix the panel.", ephemeral=True)
            else:
                await interaction.response.send_message("Inventory button failed. Please use /inventory once and I will fix the panel.", ephemeral=True)

    @discord.ui.button(label="Open Chest", style=discord.ButtonStyle.secondary, emoji="🧰")
    async def open_chest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This mission panel belongs to another player.", ephemeral=True)
            return
        try:
            await idlerpg_open.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Open Chest button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Open Chest failed. Please run /open once.", ephemeral=True)
            else:
                await interaction.response.send_message("Open Chest failed. Please run /open once.", ephemeral=True)

    @discord.ui.button(label="Gear Store", style=discord.ButtonStyle.primary, emoji="🛒")
    async def open_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This mission panel belongs to another player.", ephemeral=True)
            return
        try:
            await idlerpg_shop.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Gear Store button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Store button failed. Please run /idlerpgshop once.", ephemeral=True)
            else:
                await interaction.response.send_message("Store button failed. Please run /idlerpgshop once.", ephemeral=True)


class IdleRPGShopView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=1800)
        self.user_id = user_id

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _buy_crate(self, interaction: discord.Interaction, class_name: str):
        if not self._is_owner(interaction):
            await interaction.response.send_message("This shop panel belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create your character first with /create.", ephemeral=True)
            return

        coins = int(profile.get("money", 0))
        if coins < IDLERPG_CLASS_CRATE_COST:
            await interaction.response.send_message(
                f"Not enough coins. Class crates cost **{IDLERPG_CLASS_CRATE_COST}**. You have **{coins}**.",
                ephemeral=True,
            )
            return

        profile["money"] = coins - IDLERPG_CLASS_CRATE_COST
        reward = _idlerpg_make_class_gear(class_name, profile=profile)
        profile.setdefault("inventory", []).append(reward)
        bonus = None
        if random.random() < 0.10:
            bonus = _idlerpg_make_item(kind="chest", profile=profile)
            profile["inventory"].append(bonus)
        _save_idlerpg_data()

        reward_rarity, reward_color, reward_marker = _idlerpg_item_rarity(reward)
        if isinstance(bonus, dict):
            bonus_rarity, bonus_color, bonus_marker = _idlerpg_item_rarity(bonus)
        else:
            bonus_rarity, bonus_color, bonus_marker = ("None", 0x95A5A6, "⚪")

        embed = discord.Embed(
            title=f"{class_name} Crate Opened",
            description=f"You spent **{IDLERPG_CLASS_CRATE_COST}** coins and opened a class-themed gear crate.",
            color=reward_color,
        )
        embed.add_field(name="Reward", value=f"{reward_marker} {reward.get('emoji', '🎁')} **{reward.get('name', 'Unknown Gear')}** [{reward_rarity}]", inline=False)
        embed.add_field(name="Coins Left", value=str(profile.get("money", 0)), inline=True)
        if isinstance(bonus, dict):
            embed.add_field(name="Bonus", value=f"{bonus_marker} {bonus.get('emoji', '🧰')} **{bonus.get('name', 'Magic Chest')}** [{bonus_rarity}]", inline=True)
        else:
            embed.add_field(name="Bonus", value="None", inline=True)

        class_now = profile.get("class") or "None"
        if class_now == class_name:
            embed.add_field(name="Synergy", value="Class match bonus active for roleplay theme.", inline=False)
        else:
            embed.add_field(name="Synergy", value=f"Your current class is **{class_now}**.", inline=False)

        embed.set_footer(text="Tip: use /status to view chest count and new gear.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Warrior", style=discord.ButtonStyle.primary, emoji="⚔️", row=0)
    async def buy_warrior(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_crate(interaction, "Warrior")

    @discord.ui.button(label="Mage", style=discord.ButtonStyle.primary, emoji="🪄", row=0)
    async def buy_mage(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_crate(interaction, "Mage")

    @discord.ui.button(label="Rogue", style=discord.ButtonStyle.primary, emoji="🗡️", row=0)
    async def buy_rogue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_crate(interaction, "Rogue")

    @discord.ui.button(label="Cleric", style=discord.ButtonStyle.secondary, emoji="✨", row=1)
    async def buy_cleric(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_crate(interaction, "Cleric")

    @discord.ui.button(label="Ranger", style=discord.ButtonStyle.secondary, emoji="🏹", row=1)
    async def buy_ranger(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_crate(interaction, "Ranger")

    @discord.ui.button(label="Paladin", style=discord.ButtonStyle.secondary, emoji="🛡️", row=1)
    async def buy_paladin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_crate(interaction, "Paladin")

    @discord.ui.button(label="Open Chest", style=discord.ButtonStyle.success, emoji="🧰", row=2)
    async def shop_open_chest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            await interaction.response.send_message("This shop panel belongs to another player.", ephemeral=True)
            return
        try:
            await idlerpg_open.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Shop Open Chest failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Open Chest failed. Please run /open once.", ephemeral=True)
            else:
                await interaction.response.send_message("Open Chest failed. Please run /open once.", ephemeral=True)


class IdleRPGSacrificeAltarView(discord.ui.View):
    def __init__(self, user_id: int, items: list[dict]):
        super().__init__(timeout=900)
        self.user_id = user_id

        options: list[discord.SelectOption] = []
        for item in items[:20]:
            item_id = int(item.get("id", -1))
            gain = _idlerpg_sacrifice_gain(item)
            item_type = str(item.get("type", "item")).lower()
            label = f"{item.get('name', 'Unknown')}"
            if item_type == "chest":
                tier = str(item.get("tier", "magic")).capitalize()
                desc = f"{tier} chest • Favor +{gain} • ID {item_id}"
            elif item_type == "gear":
                desc = f"Gear • Favor +{gain} • ID {item_id}"
            else:
                desc = f"{item_type.capitalize()} • Favor +{gain} • ID {item_id}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(item_id),
                    description=desc[:100],
                    emoji=(item.get("emoji") or None),
                )
            )

        select = discord.ui.Select(
            placeholder="Select an item to offer to your god",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def _on_select(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This altar belongs to another player.", ephemeral=True)
                return

            profile = _idlerpg_profile(interaction.user)
            if not profile:
                await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
                return
            if not profile.get("god"):
                await interaction.response.send_message("Choose your god first with /follow.", ephemeral=True)
                return

            item_id = int(select.values[0])
            item = _idlerpg_find_item(profile, item_id)
            if not item:
                await interaction.response.send_message("That item is no longer in your inventory.", ephemeral=True)
                return

            gain = _idlerpg_sacrifice_gain(item)
            profile.get("inventory", []).remove(item)
            profile["favor"] = int(profile.get("favor", 0)) + gain
            _save_idlerpg_data()

            god_name = profile.get("god", "your god")
            embed = discord.Embed(
                title="Sacrifice Accepted",
                description=f"{item.get('emoji', '🎁')} **{item.get('name', 'item')}** was offered to **{god_name}**.",
                color=0xF39C12,
            )
            embed.add_field(name="Favor Gained", value=f"+{gain}", inline=True)
            embed.add_field(name="Total Favor", value=str(profile.get("favor", 0)), inline=True)
            embed.set_footer(text="Blessings from your patron deepen.")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        select.callback = _on_select
        self.add_item(select)


class IdleRPGStarterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Character Setup", style=discord.ButtonStyle.primary, emoji="🧬", custom_id="idlerpg_starter_setup")
    async def starter_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Start with /create first, then this setup panel will work.", ephemeral=True)
            return
        embed = discord.Embed(
            title="IdleRPG Character Setup",
            description="Pick your race, class, alignment, and god from the dropdowns.",
            color=0x2ECC71,
        )
        missing = _idlerpg_build_missing(profile)
        if missing:
            embed.add_field(name="Missing", value=", ".join(missing), inline=False)
        else:
            embed.add_field(name="Build", value="Complete. You can start timed missions.", inline=False)
        await interaction.response.send_message(embed=embed, view=IdleRPGBuildView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Adventure Hub", style=discord.ButtonStyle.success, emoji="🚀", custom_id="idlerpg_starter_hub")
    async def starter_hub(self, interaction: discord.Interaction, button: discord.ui.Button):
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create your character first with /create.", ephemeral=True)
            return
        embed = discord.Embed(
            title="IdleRPG Adventure Hub",
            description="Start timed missions, check timer, and open inventory from here.",
            color=0x1ABC9C,
        )
        await interaction.response.send_message(embed=embed, view=IdleRPGAdventureView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Gear Store", style=discord.ButtonStyle.primary, emoji="🛒", custom_id="idlerpg_starter_store")
    async def starter_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await idlerpg_shop.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Starter store button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Store button failed. Please run /idlerpgshop.", ephemeral=True)
            else:
                await interaction.response.send_message("Store button failed. Please run /idlerpgshop.", ephemeral=True)

    @discord.ui.button(label="Check Status", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="idlerpg_starter_status")
    async def starter_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await idlerpg_status.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Starter status button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Status button failed. Please run /status once.", ephemeral=True)
            else:
                await interaction.response.send_message("Status button failed. Please run /status once.", ephemeral=True)

    @discord.ui.button(label="Sacrifice Altar", style=discord.ButtonStyle.secondary, emoji="🙏", custom_id="idlerpg_starter_altar", row=1)
    async def starter_altar(self, interaction: discord.Interaction, button: discord.ui.Button):
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("Create your character first with /create.", ephemeral=True)
            return
        if not profile.get("god"):
            await interaction.response.send_message("Choose your god first with /follow.", ephemeral=True)
            return

        inv = profile.get("inventory", [])
        if not inv:
            await interaction.response.send_message("Your inventory is empty. Complete adventures first.", ephemeral=True)
            return

        recent = list(reversed(inv))
        embed = discord.Embed(
            title="Sacrifice Altar",
            description="Choose an item to offer your god for favor.",
            color=0xF39C12,
        )
        embed.add_field(name="God", value=profile.get("god", "None"), inline=True)
        embed.add_field(name="Favor", value=str(profile.get("favor", 0)), inline=True)
        embed.add_field(name="Items", value=str(len(inv)), inline=True)
        embed.set_footer(text="Select from your latest inventory items below.")
        await interaction.response.send_message(embed=embed, view=IdleRPGSacrificeAltarView(interaction.user.id, recent), ephemeral=True)

    @discord.ui.button(label="Help", style=discord.ButtonStyle.secondary, emoji="❓", custom_id="idlerpg_starter_help")
    async def starter_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await idlerpg_help.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Starter help button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Help button failed. Please run /idlerpghelp.", ephemeral=True)
            else:
                await interaction.response.send_message("Help button failed. Please run /idlerpghelp.", ephemeral=True)


class IdleRPGStatusPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Check My Status",
        style=discord.ButtonStyle.primary,
        emoji="📊",
        custom_id="idlerpg_status_panel_check",
    )
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await idlerpg_status.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Status panel button failed: {e}")
            if interaction.response.is_done():
                await interaction.followup.send("Status panel button failed. Please run /status once.", ephemeral=True)
            else:
                await interaction.response.send_message("Status panel button failed. Please run /status once.", ephemeral=True)


class IdleRPGRewardChoiceView(discord.ui.View):
    def __init__(self, user_id: int, item_id: int):
        super().__init__(timeout=1800)
        self.user_id = user_id
        self.item_id = int(item_id)

    def _owner_only(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Equip Drop", style=discord.ButtonStyle.success, emoji="⚔️")
    async def equip_drop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This reward belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        item = _idlerpg_find_item(profile, self.item_id) if profile else None
        if not item:
            await interaction.response.send_message("That item is no longer in your inventory.", ephemeral=True)
            return
        if item.get("type") != "gear":
            await interaction.response.send_message("Only gear can be equipped. Keep or sacrifice this drop.", ephemeral=True)
            return
        profile["equipped"] = item
        _save_idlerpg_data()
        await interaction.response.send_message(f"Equipped **{item.get('name', 'item')}**.", ephemeral=True)

    @discord.ui.button(label="Sell Drop", style=discord.ButtonStyle.danger, emoji="💰")
    async def sell_drop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This reward belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        item = _idlerpg_find_item(profile, self.item_id) if profile else None
        if not item:
            await interaction.response.send_message("That item is no longer in your inventory.", ephemeral=True)
            return
        if item.get("type") != "gear":
            await interaction.response.send_message("Only gear can be sold from this panel.", ephemeral=True)
            return
        profile.get("inventory", []).remove(item)
        coins = int(item.get("value", 50))
        profile["money"] = int(profile.get("money", 0)) + coins
        _save_idlerpg_data()
        await interaction.response.send_message(f"Sold **{item.get('name', 'item')}** for **{coins}** coins.", ephemeral=True)

    @discord.ui.button(label="Keep in Bag", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def keep_drop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This reward belongs to another player.", ephemeral=True)
            return
        await interaction.response.send_message("Kept in inventory. You can manage it later via /inventory.", ephemeral=True)

    @discord.ui.button(label="Sacrifice Drop", style=discord.ButtonStyle.primary, emoji="🙏")
    async def sacrifice_drop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This reward belongs to another player.", ephemeral=True)
            return
        profile = _idlerpg_profile(interaction.user)
        if not profile:
            await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
            return
        item = _idlerpg_find_item(profile, self.item_id)
        if not item:
            await interaction.response.send_message("That item is no longer in your inventory.", ephemeral=True)
            return
        if not profile.get("god"):
            await interaction.response.send_message("Choose your god first with /follow before offering sacrifices.", ephemeral=True)
            return

        gain = _idlerpg_sacrifice_gain(item)

        profile.get("inventory", []).remove(item)
        profile["favor"] = int(profile.get("favor", 0)) + gain
        _save_idlerpg_data()

        god_name = profile.get("god", "your god")
        await interaction.response.send_message(
            f"You offered **{item.get('name', 'item')}** to **{god_name}**. Favor +**{gain}**.",
            ephemeral=True,
        )


class IdleRPGHelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="Build Path", style=discord.ButtonStyle.success, emoji="🧱")
    async def build_path(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Build order: `/create` -> `/race` -> `/class` -> `/alignment` -> `/follow` -> `/adventure` -> `/status`.",
            ephemeral=True,
        )

    @discord.ui.button(label="Loot Loop", style=discord.ButtonStyle.primary, emoji="🎒")
    async def loot_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Loot loop: timed `/adventure` -> resolve in `/status` -> manage with `/inventory`, `/equip`, `/sell`, `/sacrifice`, `/open`.",
            ephemeral=True,
        )

    @discord.ui.button(label="Open Store", style=discord.ButtonStyle.secondary, emoji="🛒")
    async def open_store(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await idlerpg_shop.callback(interaction)
        except Exception as e:
            print(f"[IdleRPG] Help store button failed: {e}")

    @discord.ui.button(label="Open Codex", style=discord.ButtonStyle.primary, emoji="📚")
    async def open_codex(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=_idlerpg_codex_embed("overview"),
            view=IdleRPGCodexView(interaction.user.id),
            ephemeral=True,
        )


def _idlerpg_codex_embed(section: str) -> discord.Embed:
    section = (section or "overview").lower()
    if section == "build":
        embed = discord.Embed(
            title="IdleRPG Codex • Build",
            description="Character setup and progression gates.",
            color=0x2ECC71,
        )
        embed.add_field(name="Setup Path", value="`/create` `/race` `/class` `/alignment` `/follow`", inline=False)
        embed.add_field(name="Species", value=f"**{len(IDLERPG_RACES)}** total", inline=True)
        embed.add_field(name="Classes", value=f"**{len(IDLERPG_CLASSES)}** total", inline=True)
        embed.add_field(name="Alignments", value=f"**{len(IDLERPG_ALIGNMENTS)}** total", inline=True)
        embed.set_footer(text="Use buttons to view more sections.")
        return embed
    if section == "loot":
        embed = discord.Embed(
            title="IdleRPG Codex • Loot Pools",
            description="All obtainable item families and pool sizes.",
            color=0x9B59B6,
        )
        embed.add_field(name="Loot Items", value=f"**{len(IDLERPG_LOOT_POOL)}**", inline=True)
        embed.add_field(name="General Gear", value=f"**{len(IDLERPG_GEAR_POOL)}**", inline=True)
        embed.add_field(name="Class Gear", value=f"**{sum(len(v) for v in IDLERPG_CLASS_GEAR_POOLS.values())}**", inline=True)
        preview = "\n".join([f"{item[1]} {item[0]}" for item in IDLERPG_LOOT_POOL[:12]])
        embed.add_field(name="Loot Preview", value=preview or "No loot configured.", inline=False)
        embed.set_footer(text="Chests can drop additional gear/chest rewards.")
        return embed
    if section == "encounters":
        embed = discord.Embed(
            title="IdleRPG Codex • Encounters",
            description="Timed mission types and pacing.",
            color=0xE67E22,
        )
        lines = []
        for entry in IDLERPG_ENCOUNTER_TABLE:
            dmin, dmax = entry.get("duration", (0, 0))
            lines.append(
                f"{entry.get('emoji', '⚡')} **{entry.get('encounter', 'Mission')}** • x{entry.get('mult', 1.0)} • {int(dmin)//60}-{int(dmax)//60}m"
            )
        embed.add_field(name="Mission Types", value="\n".join(lines), inline=False)
        embed.set_footer(text="Resolve completed missions with /status.")
        return embed
    if section == "economy":
        embed = discord.Embed(
            title="IdleRPG Codex • Economy",
            description="Coins, favor, and item flow.",
            color=0x3498DB,
        )
        embed.add_field(name="Start", value="Level 1 • 100 coins", inline=True)
        embed.add_field(name="Class Crate", value=f"{IDLERPG_CLASS_CRATE_COST} coins", inline=True)
        embed.add_field(name="Core Commands", value="`/idlerpgshop` `/equip` `/sell` `/sacrifice` `/open`", inline=False)
        embed.set_footer(text="Favor scales with loot/chest sacrifices.")
        return embed

    embed = discord.Embed(
        title="IdleRPG Codex • Overview",
        description="Clickable index of everything currently in the game.",
        color=0x5865F2,
    )
    embed.add_field(name="Species", value=str(len(IDLERPG_RACES)), inline=True)
    embed.add_field(name="Classes", value=str(len(IDLERPG_CLASSES)), inline=True)
    embed.add_field(name="Gods", value=str(len(IDLERPG_GODS)), inline=True)
    embed.add_field(name="Alignments", value=str(len(IDLERPG_ALIGNMENTS)), inline=True)
    embed.add_field(name="Encounters", value=str(len(IDLERPG_ENCOUNTER_TABLE)), inline=True)
    embed.add_field(name="Loot Pool", value=str(len(IDLERPG_LOOT_POOL)), inline=True)
    embed.add_field(name="Navigate", value="Use the buttons below to drill into Build, Loot, Encounters, and Economy.", inline=False)
    embed.set_footer(text="IdleRPG Codex")
    return embed


class IdleRPGCodexView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=900)
        self.user_id = user_id

    def _owner_only(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.primary, emoji="📖")
    async def codex_overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This codex panel belongs to another player.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=_idlerpg_codex_embed("overview"), view=self)

    @discord.ui.button(label="Build", style=discord.ButtonStyle.success, emoji="🧬")
    async def codex_build(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This codex panel belongs to another player.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=_idlerpg_codex_embed("build"), view=self)

    @discord.ui.button(label="Loot", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def codex_loot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This codex panel belongs to another player.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=_idlerpg_codex_embed("loot"), view=self)

    @discord.ui.button(label="Encounters", style=discord.ButtonStyle.secondary, emoji="⚔️")
    async def codex_encounters(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This codex panel belongs to another player.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=_idlerpg_codex_embed("encounters"), view=self)

    @discord.ui.button(label="Economy", style=discord.ButtonStyle.secondary, emoji="💰")
    async def codex_economy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._owner_only(interaction):
            await interaction.response.send_message("This codex panel belongs to another player.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=_idlerpg_codex_embed("economy"), view=self)


_load_prestige_data()
_load_cosmetics_inventory()
_load_link_quarantine()
_load_idlerpg_data()
_load_levels_data()

# ── Personal Space (private temp voice channels) ──────────────────────────────
# guild_id -> lobby voice channel ID that triggers creation
PERSONAL_SPACE_LOBBY: dict[int, int] = {}
# channel_id -> owner member_id  (tracks active personal space channels)
PERSONAL_SPACE_CHANNELS: dict[int, int] = {}

# ── Game Channel System ───────────────────────────────────────────────────────
GAME_LIST = [
    ("Arc Raiders",     "🔫"),
    ("Fortnite",         "ðŸ…"),
    ("Phasmophobia",     "👻"),
    ("Minecraft",        "â›ï¸"),
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
                embed.add_field(name="👤 Closed by", value=interaction.user.mention, inline=True)
                embed.add_field(name="📦 Messages", value=str(len(msgs)), inline=True)
                embed.description = f"```\n{transcript[:3900]}\n```" if transcript else "*No messages.*"
                embed.set_footer(text="Ticket Transcript • Gaming Zone")
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
                    "âŒ Could not find your member record. Please try again in a moment.", ephemeral=True
                )
                return
        gamer_role = discord.utils.get(interaction.guild.roles, name=GAMER_ROLE_NAME)
        if not gamer_role:
            await interaction.followup.send(
                "âŒ The Gamer role doesn't exist yet — ask an admin to run `/setupchannels`.",
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
                "âŒ I don't have permission to assign roles. Ask an admin to move my bot role **above** the Gamer role in Server Settings → Roles.",
                ephemeral=True,
            )
            print(f"[Verify] FORBIDDEN — bot role is below @{GAMER_ROLE_NAME} in hierarchy. Cannot assign role.")
            return
        except Exception as e:
            await interaction.followup.send(
                "âŒ Something went wrong assigning your role. Please try again or contact an admin.", ephemeral=True
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

    @discord.ui.button(label="Maybe", emoji="â”", style=discord.ButtonStyle.secondary)
    async def btn_maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_choice(interaction, "maybe")

    @discord.ui.button(label="Pass", emoji="âŒ", style=discord.ButtonStyle.secondary)
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
                title="🎫 Gaming Zone Support",
                description=(
                    f"Hey {member.mention}, your private support channel is ready.\n\n"
                    "Share what happened, screenshots, and any error text so staff can help faster."
                ),
                color=0x5865F2,
            )
            embed.add_field(name="⚡ Fastest Path", value="Include your steps + what you expected to happen.", inline=False)
            embed.add_field(name="🔒 Privacy", value="Only you and staff can see this channel.", inline=False)
            embed.set_footer(text="Use the Close Ticket button when resolved.")
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
    alert_ch = _resolve_streamer_channel(guild)
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
            description="ðŸ” No free games found right now on Steam or Epic. The bot checks every 4 hours — we'll post automatically when deals appear!",
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
        total_xp = XP_DATA.get(gid, {}).get(uid, 0)
        current_level = _xp_to_level(total_xp)
        next_level = current_level + 1
        xp_for_next = _xp_required(next_level)
        xp_this_level = total_xp - sum(_xp_required(l) for l in range(current_level))
        progress = min(1.0, xp_this_level / xp_for_next) if xp_for_next else 0.0
        bar = "▰" * int(progress * 18) + "▱" * (18 - int(progress * 18))
        gain_msg = (
            f"✨ {message.author.mention} +{XP_PER_MSG} XP • Level {current_level} • {total_xp} total XP\n"
            f"Progress to Level {next_level}\n{bar} {xp_this_level}/{xp_for_next} XP"
        )
        if XP_GAIN_FEED_ENABLED:
            try:
                level_ch = _resolve_levels_channel(message.guild)
                gain_target = level_ch or message.channel
                await gain_target.send(gain_msg)
            except Exception:
                pass
        if leveled_up:
            try:
                level_embed = _build_gz_levelup_card(
                    message.guild,
                    message.author,
                    new_lvl,
                    total_xp,
                    xp_this_level,
                    xp_for_next,
                )
                level_embed.set_footer(text="Gaming Zone Level Card • Auto-removes soon")
                await message.channel.send(
                    content=message.author.mention,
                    embed=level_embed,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    delete_after=12,
                )
            except Exception:
                pass

    # ── Auto-moderation ───────────────────────────────────────────────────────
    # Skip whitelisted users and admins
    if message.author.id not in WHITELIST and not message.author.guild_permissions.administrator:
        content_lower = message.content.lower()

        # Banned word filter — use word boundaries to avoid false positives like "nig" in "Goodnight"
        has_banned_word = False
        for word in BANNED_WORDS:
            if re.search(rf'\b{re.escape(word)}\b', content_lower):
                has_banned_word = True
                break
        
        if has_banned_word:
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
                    f"{message.author.mention} âš ï¸ **Warning 1/2:** Your message was removed for containing a banned word. "
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

        streamer_channel = _resolve_or_track_text_channel(
            message.guild,
            "streamer_channel",
            STREAMER_CHANNEL_NAME,
            "streamers",
            "streamer-channel",
        )
        allow_stream_post = (
            streamer_channel is not None
            and message.channel.id == streamer_channel.id
            and _contains_stream_link(message.content)
        )

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

        if is_ad and not allow_stream_post:
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
                    f"{message.author.mention} âš ï¸ **Warning 1/2:** Advertising other Discord servers or bots is not allowed.\n"
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
                        embed = discord.Embed(title="ðŸ” Auto-Mod: Account Age Gate (Link Blocked)", color=0x3498DB)
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
                        f"{message.author.mention} âš ï¸ **Message removed:** Suspicious link detected (requires staff review). "
                        f"A second offense will result in a **ban**.",
                        delete_after=8,
                    )
                    log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        embed = discord.Embed(title="âš ï¸ Auto-Mod: Suspicious Link Detected (Level 1)", color=0xFF6B6B)
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
    embed  = discord.Embed(title=f"ðŸ·ï¸ {action}", color=color)
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
    embed = discord.Embed(title=f"âš™ï¸ Admin Command: `/{cmd}`", color=0x5865F2)
    embed.add_field(name="Admin",   value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
    embed.add_field(name="Channel", value=f"<#{interaction.channel_id}>",                          inline=True)
    if details:
        embed.add_field(name="Details", value=details, inline=False)
    embed.timestamp = discord.utils.utcnow()
    await ch.send(embed=embed)


@client.tree.command(name="ban", description="Ban a server member")
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

@client.tree.command(name="unban", description="Unban a user by their ID")
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

@client.tree.command(name="banlist", description="Show all currently banned users")
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

@client.tree.command(name="kick", description="Kick a server member")
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

@client.tree.command(name="mute", description="Mute a member for a number of seconds")
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

@client.tree.command(name="unmute", description="Unmute a member")
@app_commands.default_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to unmute members.", ephemeral=True)
        return
    await user.timeout(None)
    await interaction.response.send_message(f'Unmuted {user}.')
    await log_action(interaction, "Unmute", f"{user} ({user.id})")

@client.tree.command(name="timeout", description="Timeout a member for a set number of hours and minutes")
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
    me = interaction.guild.me
    if me is None or not me.guild_permissions.moderate_members:
        await interaction.response.send_message(
            "I cannot timeout members right now. Give me the **Moderate Members** permission.",
            ephemeral=True,
        )
        await log_action(interaction, "Timeout Failed", f"{user} ({user.id})", "Bot missing Moderate Members permission")
        return
    if user.top_role >= me.top_role:
        await interaction.response.send_message(
            "I cannot timeout that user because their top role is higher than or equal to mine.",
            ephemeral=True,
        )
        await log_action(interaction, "Timeout Failed", f"{user} ({user.id})", "Target role is above bot role")
        return
    if user.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message(
            "You cannot timeout someone with a role equal to or higher than yours.",
            ephemeral=True,
        )
        return
    try:
        await user.timeout(timedelta(seconds=total_seconds), reason=reason)
        await interaction.response.send_message(f"Timed out {user} for {hours}h {minutes}m.")
        await log_action(interaction, "Timeout", f"{user} ({user.id})", f"{hours}h {minutes}m — {reason}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "Timeout failed: missing permission or role hierarchy does not allow this action.",
            ephemeral=True,
        )
        await log_action(
            interaction,
            "Timeout Failed",
            f"{user} ({user.id})",
            f"{hours}h {minutes}m — {reason} | Forbidden (permissions/hierarchy)",
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"Timeout failed due to an API error: {e}",
            ephemeral=True,
        )
        await log_action(
            interaction,
            "Timeout Failed",
            f"{user} ({user.id})",
            f"{hours}h {minutes}m — {reason} | HTTPException: {e}",
        )

@client.tree.command(name="whitelist", description="Whitelist a member to protect them from mod actions")
@app_commands.default_permissions(administrator=True)
async def whitelist(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You don't have permission to whitelist members.", ephemeral=True)
        return
    WHITELIST.add(user.id)
    await interaction.response.send_message(f'{user} has been whitelisted.')
    await log_action(interaction, "Whitelist", f"{user} ({user.id})")

@client.tree.command(name="unwhitelist", description="Remove a member from the whitelist")
@app_commands.default_permissions(administrator=True)
async def unwhitelist(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You don't have permission to manage the whitelist.", ephemeral=True)
        return
    WHITELIST.discard(user.id)
    await interaction.response.send_message(f'{user} has been removed from the whitelist.')
    await log_action(interaction, "Unwhitelist", f"{user} ({user.id})")

@client.tree.command(name="clear", description="Clear messages from the channel")
@app_commands.default_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: int = 10):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You don't have permission to manage messages.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount) # pyright: ignore[reportAttributeAccessIssue]
    await interaction.followup.send(f'Cleared {len(deleted)} messages.', ephemeral=True)
    await log_action(interaction, "Clear", f"Channel: {interaction.channel.name}", f"Deleted: {len(deleted)} messages")


@client.tree.command(name="warnings", description="View a member's current warning count")
@app_commands.default_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to view warnings.", ephemeral=True)
        return

    banned_word_count = BANNED_WORD_WARNINGS.get(user.id, 0)
    ad_count = AD_WARNINGS.get(user.id, 0)
    total = banned_word_count + ad_count

    embed = discord.Embed(
        title="âš ï¸ Member Warning Summary",
        color=0xF1C40F if total > 0 else 0x2ECC71,
    )
    embed.add_field(name="Member", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Banned Word Warnings", value=str(banned_word_count), inline=True)
    embed.add_field(name="Advertising Warnings", value=str(ad_count), inline=True)
    embed.add_field(name="Total Active Warnings", value=str(total), inline=False)
    embed.set_footer(text="These are active auto-mod warning counters.")

    await interaction.response.send_message(embed=embed)


@client.tree.command(name="clearwarnings", description="Clear a member's active warning counters")
@app_commands.default_permissions(moderate_members=True)
async def clearwarnings(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to clear warnings.", ephemeral=True)
        return

    had_banned_word_warning = user.id in BANNED_WORD_WARNINGS
    had_ad_warning = user.id in AD_WARNINGS

    BANNED_WORD_WARNINGS.pop(user.id, None)
    AD_WARNINGS.pop(user.id, None)

    if had_banned_word_warning or had_ad_warning:
        await interaction.response.send_message(
            f"✅ Cleared active warnings for {user.mention}.\n"
            f"Removed: banned words = {'yes' if had_banned_word_warning else 'no'}, "
            f"advertising = {'yes' if had_ad_warning else 'no'}."
        )
        await log_action(interaction, "Clear Warnings", f"{user} ({user.id})")
    else:
        await interaction.response.send_message(f"{user.mention} has no active warnings to clear.", ephemeral=True)

# ── Phase 4: Moderation Safety (Raid Mode, Link Quarantine, Account Age Gate) ──
@client.tree.command(name="raidmode", description="Toggle raid mode lockdown (Admin only)")
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

@client.tree.command(name="accountage", description="Set minimum account age gate (Admin only)")
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

@client.tree.command(name="linkquarantine", description="View or clear link quarantine records (Admin only)")
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
        embed = discord.Embed(title="ðŸ” Link Quarantine Records", color=0xFF6B6B)
        lines = []
        for uid, record in sorted(LINK_QUARANTINE.items()):
            q_level = record.get("quarantine_level", 0)
            level_text = {0: "None", 1: "âš ï¸ Quarantine", 2: "🚫 Banned"}
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
    'before_options': '-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_delay_max 10 -rw_timeout 15000000',
    # discord.py already sets PCM output flags (-f s16le -ar 48000 -ac 2).
    # Avoid duplicating them here to prevent FFmpeg option conflicts.
    'options': '-vn -sn -dn -bufsize 256k',
}

YTDL_STREAM_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'format': 'bestaudio[ext=m4a]/bestaudio/best',
    'source_address': '0.0.0.0',
    'extractor_args': {'youtube': {'player_client': ['android', 'web', 'tv_embedded']}},
}

class SongEntry:
    def __init__(self, title: str, url: str, webpage_url: str, duration: int, requester: discord.Member, local_path: str | None = None):
        self.title = title
        self.url = url
        self.webpage_url = webpage_url
        try:
            self.duration = int(float(duration or 0))
        except (TypeError, ValueError):
            self.duration = 0
        self.requester = requester
        self.local_path = local_path

    def format_duration(self) -> str:
        duration = int(self.duration or 0)
        m, s = divmod(duration, 60)
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
        self.source_transformer: discord.PCMVolumeTransformer | None = None
        self.autoplay: bool = False
        self.autoplay_mode: str = "gzvibe"
        self.last_autoplay_debug: list[dict] = []
        self.retry_attempts: dict[str, int] = {}

music_states: dict[int, GuildMusicState] = {}

def get_music_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]


def _music_data_dir() -> str:
    explicit = os.getenv("MUSIC_DATA_DIR", "").strip()
    if explicit:
        os.makedirs(explicit, exist_ok=True)
        return explicit
    railway_mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if railway_mount:
        os.makedirs(railway_mount, exist_ok=True)
        return railway_mount
    if os.path.isdir("/data"):
        return "/data"
    return os.path.dirname(os.path.abspath(__file__))


_PLAYLISTS_PATH = os.path.join(_music_data_dir(), "music_playlists.json")
MUSIC_PLAYLISTS: dict[str, dict] = {"guilds": {}}


def _load_music_playlists() -> None:
    global MUSIC_PLAYLISTS
    if not os.path.exists(_PLAYLISTS_PATH):
        return
    try:
        with open(_PLAYLISTS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict) and isinstance(loaded.get("guilds", {}), dict):
            MUSIC_PLAYLISTS = loaded
    except Exception as e:
        print(f"[Playlist] Failed to load playlists: {e}")


def _save_music_playlists() -> None:
    try:
        os.makedirs(os.path.dirname(_PLAYLISTS_PATH), exist_ok=True)
        tmp_path = _PLAYLISTS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(MUSIC_PLAYLISTS, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _PLAYLISTS_PATH)
    except Exception as e:
        print(f"[Playlist] Failed to save playlists: {e}")


def _playlist_bucket(guild_id: int, user_id: int) -> dict[str, list[dict]]:
    guilds = MUSIC_PLAYLISTS.setdefault("guilds", {})
    g = guilds.setdefault(str(guild_id), {})
    return g.setdefault(str(user_id), {})


def _playlist_track(song: SongEntry) -> dict:
    return {
        "title": song.title,
        "webpage_url": song.webpage_url or "",
        "duration": int(song.duration or 0),
    }


def _normalize_playlist_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())[:40]


def _playlist_has_track(tracks: list[dict], track: dict) -> bool:
    track_url = (track.get("webpage_url") or "").strip()
    track_title = (track.get("title") or "").strip().casefold()
    for item in tracks:
        item_url = (item.get("webpage_url") or "").strip()
        item_title = (item.get("title") or "").strip().casefold()
        if track_url and item_url and track_url == item_url:
            return True
        if track_title and item_title and track_title == item_title:
            return True
    return False


def _gzvibe_playlist_embed(title: str, description: str, color: int = 0x1DB954) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="GzVibe Playlist • Gaming Zone")
    return embed


_load_music_playlists()


def _youtube_video_id(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            return (parsed.path or "").lstrip("/").split("/")[0]
        if "youtube.com" in host or "youtube-nocookie.com" in host:
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("v"):
                return q["v"][0]
            if q.get("id"):
                return q["id"][0]
            path_parts = [p for p in (parsed.path or "").split("/") if p]
            if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live", "v"}:
                return path_parts[1]
        if "googlevideo.com" in host:
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("id"):
                return q["id"][0]
    except Exception:
        return ""
    return ""


def _youtube_thumbnail(url: str) -> str:
    video_id = _youtube_video_id(url)
    if not video_id:
        return ""
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


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


def _autoplay_query_seed(song: SongEntry) -> str:
    """Build a cleaner autoplay search seed than raw YouTube titles."""
    artist = _song_artist_key(song)
    core = _song_core_key(song.title)
    if artist and core:
        return f"{artist} {core}"
    return core or artist or song.title


def _autoplay_title_tokens(title: str) -> set[str]:
    core = _song_core_key(title)
    if not core:
        return set()
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with"}
    return {t for t in core.split() if len(t) >= 3 and t not in stop}


def _autoplay_noise_penalty(title: str) -> int:
    lowered = (title or "").lower()
    penalty = 0
    noisy_terms = {
        "slowed": -5,
        "reverb": -4,
        "sped up": -5,
        "nightcore": -6,
        "8d": -6,
        "bass boosted": -4,
        "full album": -8,
        "playlist": -8,
        "karaoke": -7,
        "reaction": -7,
        "compilation": -6,
    }
    for term, points in noisy_terms.items():
        if term in lowered:
            penalty += points
    return penalty


def _autoplay_candidate_score(
    entry: dict,
    *,
    current: "SongEntry",
    recent_title_keys: collections.deque[str],
    recent_artist_keys: collections.deque[str],
    queue_artist_keys: set[str],
    current_artist_key: str,
    autoplay_mode: str,
    source_bias: int,
) -> int:
    score = source_bias
    title = entry.get("title", "")
    candidate_tokens = _autoplay_title_tokens(title)
    current_tokens = set(_song_signature_tokens(current.title))
    maki_mode = autoplay_mode == "gzvibe"

    if current_tokens and candidate_tokens:
        token_matches = len(candidate_tokens.intersection(current_tokens))
        # Small overlap can be useful for vibe continuity, but heavy overlap
        # usually means the same song family instead of a genuinely new track.
        if token_matches == 1:
            score += 2 if maki_mode else 1
        elif token_matches >= 2:
            score -= min(10 if maki_mode else 8, token_matches * (4 if maki_mode else 3))

    for recent_key in list(recent_title_keys)[-3:]:
        recent_tokens = {token for token in recent_key.split() if len(token) >= 3}
        if recent_tokens:
            overlap = len(candidate_tokens.intersection(recent_tokens))
            if overlap:
                score -= min(6 if maki_mode else 4, overlap * (2 if maki_mode else 1))

    candidate_artist = _entry_artist_key(entry)
    if candidate_artist:
        repeat_count = sum(1 for artist in recent_artist_keys if artist == candidate_artist)
        if current_artist_key and candidate_artist == current_artist_key:
            if maki_mode:
                # GzVibe: heavily reward same artist — that's the whole point
                score += 20
            else:
                # Balanced: small same-artist boost, but penalise if they're already recurring
                score += 6 if repeat_count == 0 else max(0, 6 - repeat_count * 3)
        elif candidate_artist in queue_artist_keys:
            score += 8 if maki_mode else 5
        if maki_mode:
            # GzVibe: penalise artists that aren't current and aren't queued
            if candidate_artist not in queue_artist_keys and candidate_artist != current_artist_key:
                score -= 5
            # Mild repeat penalty even for same-artist (avoid literal looping)
            if repeat_count >= 3:
                score -= min(8, repeat_count * 2)
            elif repeat_count == 1:
                score += 1
        else:
            # Balanced: reward fresh/new artists to encourage discovery
            if repeat_count == 0:
                score += 5
            elif repeat_count == 1:
                score += 2
            elif repeat_count >= 2:
                score -= min(10, repeat_count * 3)
            # Extra bonus if this is a brand-new artist not seen in queue either
            if candidate_artist not in queue_artist_keys and candidate_artist != current_artist_key and repeat_count == 0:
                score += 4
    else:
        score -= 4 if maki_mode else 2

    candidate_duration = int(entry.get("duration") or 0)
    current_duration = int(current.duration or 0)
    if candidate_duration and current_duration:
        longer = max(candidate_duration, current_duration)
        shorter = max(1, min(candidate_duration, current_duration))
        ratio = longer / shorter
        if ratio <= 1.35:
            score += 4
        elif ratio <= 2.0:
            score += 2
        elif ratio >= 4.0:
            score -= 6 if maki_mode else 4

    score += _autoplay_noise_penalty(title)
    if maki_mode and source_bias < 12:
        score -= 3
    return score


def _format_autoplay_mode(mode: str) -> str:
    return "GzVibe" if mode == "gzvibe" else "Balanced"


def _autoplay_mode_button_style(mode: str) -> discord.ButtonStyle:
    return discord.ButtonStyle.success if mode == "gzvibe" else discord.ButtonStyle.secondary


def _autoplay_mode_embed(mode: str) -> discord.Embed:
    is_gzvibe = mode == "gzvibe"
    embed = discord.Embed(
        title="ðŸŽšï¸ Autoplay Mode",
        color=0xF1C40F if is_gzvibe else 0x5865F2,
        description=(
            "**GzVibe** keeps the queue in the same lane with tighter artist/style continuity."
            if is_gzvibe
            else "**Balanced** explores broader recommendations while still avoiding repeats."
        ),
    )
    embed.add_field(name="Current", value=f"**{_format_autoplay_mode(mode)}**", inline=True)
    embed.add_field(name="Switch", value="Use `/autoplaymode` or the mode button on the music panel.", inline=True)
    embed.add_field(
        name="Visual Preset",
        value=(
            "`High Cohesion` for GzVibe"
            if is_gzvibe
            else "`Discovery Mix` for Balanced"
        ),
        inline=False,
    )
    embed.set_footer(text="GzVibe Engine • Music UX")
    return embed


def _summarize_autoplay_debug(entry: dict) -> dict:
    duration = int(entry.get("duration") or 0)
    m, s = divmod(duration, 60)
    h, m = divmod(m, 60)
    return {
        "title": entry.get("title", "Unknown title"),
        "source": entry.get("source", "unknown"),
        "score": entry.get("score", 0),
        "artist": entry.get("artist", "Unknown"),
        "duration": f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}",
    }


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
    """Pytubefix is unreliable due to YouTube bot detection; go straight to yt-dlp."""
    print(f'[Search] Skipping pytubefix (bot-detected), using yt-dlp for: {query!r}')
    return _ytdlp_search(query, max_results)


def _normalize_search_query(query: str) -> str:
    """Deduplicate repeated words to avoid noisy queries like 'lyrics lyrics audio'."""
    parts = re.split(r"\s+", (query or "").strip())
    if not parts:
        return ""
    out: list[str] = []
    seen: set[str] = set()
    for token in parts:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return " ".join(out)


_resolved_cookiefile_cache: str | None = None
_resolved_cookiefile_checked: bool = False

def _resolve_yt_cookiefile() -> str | None:
    """Resolve yt-dlp cookie file from path env or base64 env payload.
    Result is cached after the first successful resolution so we never
    re-decode the base64 blob or re-write the file on subsequent calls."""
    global _resolved_cookiefile_cache, _resolved_cookiefile_checked
    if _resolved_cookiefile_checked:
        return _resolved_cookiefile_cache

    _resolved_cookiefile_checked = True

    cookiefile = os.getenv("YTDLP_COOKIES_PATH", "").strip()
    if cookiefile and os.path.exists(cookiefile):
        _resolved_cookiefile_cache = cookiefile
        return _resolved_cookiefile_cache

    b64_blob = os.getenv("YTDLP_COOKIES_B64", "").strip()
    if not b64_blob:
        return None

    try:
        data = base64.b64decode(b64_blob)
        if not data:
            return None
        target = os.path.join(_music_data_dir(), "yt_cookies.txt")
        with open(target, "wb") as f:
            f.write(data)
        if os.path.exists(target):
            print(f"[yt-dlp] Loaded cookies from YTDLP_COOKIES_B64 into {target}")
            _resolved_cookiefile_cache = target
            return _resolved_cookiefile_cache
    except Exception as e:
        print(f"[yt-dlp] Failed to decode YTDLP_COOKIES_B64: {e}")
    return None


def _piped_search(query: str, max_results: int) -> list[dict]:
    """Fallback using public Piped API instances."""
    instances = [
        'https://piped.video',
        'https://piped.adminforge.de',
        'https://piped.projectsegfau.lt',
    ]

    for base in instances:
        try:
            search_url = f"{base}/api/v1/search?q={urllib.parse.quote(query)}&filter=videos"
            req = urllib.request.Request(
                search_url,
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Accept': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode(errors='ignore'))

            results = []
            for item in data[: max_results * 4]:
                vid = item.get('url') or item.get('id') or ''
                if not vid:
                    continue
                vid = vid.replace('/watch?v=', '').replace('https://www.youtube.com/watch?v=', '').strip('/ ')
                if not vid:
                    continue
                streams_url = f"{base}/api/v1/streams/{vid}"
                req2 = urllib.request.Request(
                    streams_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Accept': 'application/json',
                    },
                )
                with urllib.request.urlopen(req2, timeout=6) as resp2:
                    details = json.loads(resp2.read().decode(errors='ignore'))

                audio_streams = details.get('audioStreams', [])
                if not audio_streams:
                    continue
                best = max(audio_streams, key=lambda s: int(s.get('bitrate') or 0))
                stream_url = best.get('url')
                if not stream_url:
                    continue

                results.append({
                    'title': item.get('title', 'Unknown title'),
                    'url': stream_url,
                    'webpage_url': f"https://www.youtube.com/watch?v={vid}",
                    'duration': int(item.get('duration') or 0),
                })
                if len(results) >= max_results:
                    break

            if results:
                print(f"[Piped] Resolved {len(results)} result(s) via {base}")
                return results
        except Exception as e:
            print(f"[Piped] instance failed {base}: {e}")
            continue

    return []


def _ytdlp_search(query: str, max_results: int) -> list[dict]:
    """Fallback: use yt-dlp search and resolve a playable audio URL."""
    try:
        import yt_dlp

        query = _normalize_search_query(query)
        cookiefile = _resolve_yt_cookiefile()
        base_opts = {
            'quiet': True,
            'no_warnings': True,
            'default_search': 'ytsearch',
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
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
                                    webpage_url = entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
                                    # Keep search lightweight: carry a watch URL when direct stream
                                    # URL is unavailable; playback will resolve a fresh stream later.
                                    stream_url = entry.get('url')
                                    if not stream_url or 'youtube.com/watch' in str(stream_url):
                                        stream_url = webpage_url
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

        piped = _piped_search(query, max_results)
        if piped:
            return piped
        return _invidious_search(query, max_results)
    except Exception as e:
        print(f'[yt-dlp] failed: {e}')
        piped = _piped_search(query, max_results)
        if piped:
            return piped
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

        cookiefile = _resolve_yt_cookiefile()
        base_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'http_headers': {'User-Agent': 'Mozilla/5.0'},
            'source_address': '0.0.0.0',
        }
        if cookiefile and os.path.exists(cookiefile):
            base_opts['cookiefile'] = cookiefile

        attempts = [
            {**base_opts, 'format': 'bestaudio[ext=m4a]/bestaudio/best'},
            {**base_opts, 'format': 'bestaudio/best'},
            base_opts,
        ]

        for ydl_opts in attempts:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)

                # Playlist-ish responses can still happen; use first entry if present.
                if info and info.get('entries'):
                    info = next((e for e in info.get('entries', []) if e), None)
                if not info:
                    continue

                stream_url = info.get('url')
                if not stream_url:
                    formats = info.get('formats') or []
                    audio_formats = [
                        f for f in formats
                        if f.get('url') and str(f.get('acodec', 'none')) != 'none'
                    ]
                    if audio_formats:
                        best_audio = max(audio_formats, key=lambda f: int(f.get('abr') or f.get('tbr') or 0))
                        stream_url = best_audio.get('url')

                if not stream_url:
                    stream_url = info.get('webpage_url') or url
                if not stream_url:
                    continue

                return [{
                    'title': info.get('title', 'Unknown title'),
                    'url': stream_url,
                    'webpage_url': info.get('webpage_url') or url,
                    'duration': info.get('duration', 0) or 0,
                }]
            except Exception as e:
                print(f"[yt-dlp] direct url attempt failed: {e}")
                continue

        return []
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
        lowered = q.lower()
        attempts.extend([
            f"{q} official audio" if "official audio" not in lowered else q,
            f"{q} topic" if "topic" not in lowered else q,
            f"{q} lyrics" if "lyrics" not in lowered else q,
        ])

    seen: set[str] = set()
    for attempt in attempts:
        normalized_attempt = _normalize_search_query(attempt)
        key = normalized_attempt.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        results = await search_youtube(normalized_attempt, max_results=max_results)
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
            'ignoreerrors': True,
            'noplaylist': True,
        }
        cookiefile = _resolve_yt_cookiefile()
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
            if len(results) >= 12:
                break
        return results
    except Exception as e:
        print(f"[Autoplay] yt-dlp related fetch failed: {e}")
        return []


async def fetch_related_song(state: "GuildMusicState", current: "SongEntry") -> dict | None:
    """Fetch a genuine related song for autoplay using the current ranking mode."""
    ranked = await _rank_autoplay_candidates(state, current)
    state.last_autoplay_debug = [
        _summarize_autoplay_debug({
            **entry,
            "source": source_name,
            "score": score,
            "artist": _entry_artist_key(entry) or "Unknown",
        })
        for score, entry, source_name in ranked[:5]
    ]

    if ranked:
        recent_ids = set(state.recent_track_ids)
        current_id = _song_identity(current)
        if current_id:
            recent_ids.add(current_id)

        recent_title_keys = set(state.recent_title_keys)
        current_key = _song_core_key(current.title)
        if current_key:
            recent_title_keys.add(current_key)

        seed_titles = [current.title]
        if state.last_finished and state.last_finished.title:
            seed_titles.append(state.last_finished.title)

        for score, entry, source_name in ranked:
            candidate_id = _entry_identity(entry)
            if candidate_id and candidate_id in recent_ids:
                continue

            candidate_title = entry.get("title", "")
            if not candidate_title:
                continue

            candidate_key = _song_core_key(candidate_title)
            if candidate_key and any(_same_song_key(candidate_key, key) for key in recent_title_keys):
                continue

            if any(_titles_too_similar(seed_title, candidate_title) for seed_title in seed_titles if seed_title):
                continue

            candidate_url = entry.get("webpage_url") or ""
            if candidate_url and current.webpage_url and candidate_url == current.webpage_url:
                continue

            print(
                f"[Autoplay] Picked {source_name} candidate "
                f"({score}, mode={state.autoplay_mode}): {candidate_title}"
            )
            return entry

        print(f"[Autoplay] Ranked {len(ranked)} candidates but all were repeat-like; no pick made.")
    else:
        print(f"[Autoplay] No ranked candidates available for: {current.title}")

    # Last resort: keep playback alive by selecting a best-effort candidate
    # from broad artist/title searches with relaxed filtering.
    fallback = await _autoplay_last_chance_pick(state, current)
    if fallback:
        print(f"[Autoplay] Last-chance fallback pick: {fallback.get('title', 'Unknown title')}")
        return fallback
    return None


async def _autoplay_last_chance_pick(state: "GuildMusicState", current: "SongEntry") -> dict | None:
    """Best-effort fallback when normal autoplay ranking cannot pick a track."""
    artist_key = _song_artist_key(current)
    query_seed = _autoplay_query_seed(current)
    # Emergency fallback should prefer different songs by the same or adjacent
    # artist, not alternate uploads/versions of the current song.
    if artist_key:
        queries = [
            f"{artist_key} best songs",
            f"{artist_key} radio",
            f"artists similar to {artist_key} best songs",
        ]
    else:
        queries = [
            f"songs like {query_seed}",
            f"music similar to {query_seed}",
        ]

    recent_ids = set(state.recent_track_ids)
    current_id = _song_identity(current)
    if current_id:
        recent_ids.add(current_id)

    recent_title_keys = set(state.recent_title_keys)
    current_key = _song_core_key(current.title)
    if current_key:
        recent_title_keys.add(current_key)

    async def _safe_search(query: str) -> list[dict]:
        try:
            normalized = _normalize_search_query(query)
            return await asyncio.wait_for(search_youtube(normalized, max_results=10), timeout=8.0)
        except Exception:
            return []

    result_sets = await asyncio.gather(*[_safe_search(q) for q in queries])

    best_strict: tuple[int, dict] | None = None
    for query, results in zip(queries, result_sets):
        for entry in results:
            title = entry.get("title", "")
            if not title:
                continue

            candidate_id = _entry_identity(entry)
            if candidate_id and candidate_id in recent_ids:
                continue

            candidate_url = entry.get("webpage_url") or ""
            if candidate_url and current.webpage_url and candidate_url == current.webpage_url:
                continue

            candidate_key = _song_core_key(title)

            score = _autoplay_candidate_score(
                entry,
                current=current,
                recent_title_keys=state.recent_title_keys,
                recent_artist_keys=state.recent_artist_keys,
                queue_artist_keys=set(),
                current_artist_key=artist_key,
                autoplay_mode=state.autoplay_mode,
                source_bias=9,
            )

            if "radio" in query.lower() or "best songs" in query.lower():
                score += 2
            if "artists similar to" in query.lower():
                score += 1

            strict_allowed = True
            if candidate_key and any(_same_song_key(candidate_key, key) for key in recent_title_keys):
                strict_allowed = False
            if strict_allowed and _titles_too_similar(current.title, title):
                strict_allowed = False

            if strict_allowed:
                if best_strict is None or score > best_strict[0]:
                    best_strict = (score, entry)

    if best_strict:
        return best_strict[1]

    return None


async def _rank_autoplay_candidates(state: "GuildMusicState", current: "SongEntry") -> list[tuple[int, dict, str]]:
    """Build a ranked list of autoplay candidates so runtime and debug share the same logic."""
    loop = asyncio.get_running_loop()
    exclude = current.webpage_url or current.url
    query_seed = _autoplay_query_seed(current)
    blocked_ids: set[str] = set(state.recent_track_ids)
    blocked_title_keys: set[str] = set(state.recent_title_keys)
    current_id = _song_identity(current)
    if current_id:
        blocked_ids.add(current_id)
    current_title_key = _song_core_key(current.title)
    if current_title_key:
        blocked_title_keys.add(current_title_key)
    current_artist_key = _song_artist_key(current)
    seed_tokens = _song_signature_tokens(current.title)
    recent_seed_titles: list[str] = [current.title]
    if state.last_finished and state.last_finished.title:
        recent_seed_titles.append(state.last_finished.title)
    for q_item in list(state.queue)[:2]:
        if q_item and q_item.title:
            recent_seed_titles.append(q_item.title)
    queue_artist_keys: set[str] = set()
    for q_item in state.queue:
        qid = _song_identity(q_item)
        if qid:
            blocked_ids.add(qid)
        qkey = _song_core_key(q_item.title)
        if qkey:
            blocked_title_keys.add(qkey)
        qartist = _song_artist_key(q_item)
        if qartist:
            queue_artist_keys.add(qartist)

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
        if any(_titles_too_similar(seed_title, rtitle) for seed_title in recent_seed_titles if seed_title):
            return False

        # Guard against same-song remixes/covers while avoiding false negatives.
        # Keep this strict only for richer signatures; short signatures like
        # "rich spirit" can otherwise reject almost all related candidates.
        if seed_tokens:
            rtokens = set(t for t in _song_core_key(rtitle).split() if t)
            if len(seed_tokens) >= 4 and sum(1 for t in seed_tokens[:4] if t in rtokens) >= 3:
                return False

        if rtitle.lower() == current.title.lower():
            return False

        return True

    seen_candidates: set[str] = set()
    scored_candidates: list[tuple[int, dict, str]] = []

    def _consider_candidates(entries: list[dict], *, source_bias: int, source_name: str) -> None:
        for entry in entries:
            if not _candidate_allowed(entry):
                continue
            candidate_key = _entry_identity(entry) or entry.get("webpage_url") or entry.get("title", "").lower()
            if not candidate_key or candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)
            score = _autoplay_candidate_score(
                entry,
                current=current,
                recent_title_keys=state.recent_title_keys,
                recent_artist_keys=state.recent_artist_keys,
                queue_artist_keys=queue_artist_keys,
                current_artist_key=current_artist_key,
                autoplay_mode=state.autoplay_mode,
                source_bias=source_bias,
            )
            scored_candidates.append((score, entry, source_name))

    # 1) Try pulling YouTube related videos via yt-dlp
    if exclude:
        related = await loop.run_in_executor(None, lambda: _fetch_related_yt_dlp(exclude, exclude))
        _consider_candidates(
            related,
            source_bias=38 if state.autoplay_mode == "gzvibe" else 30,
            source_name="related",
        )

    # 2) Fallback search pool — run all queries in parallel with a per-search cap
    # so a single slow yt-dlp call can't block the next song from starting.
    try:
        if state.autoplay_mode == "gzvibe":
            # GzVibe: same artist first, then similar artists, then song-based
            radio_queries = [
                f"{current_artist_key} radio" if current_artist_key else f"{query_seed} radio",
                f"{current_artist_key} best songs" if current_artist_key else f"{query_seed} radio",
            ]
            if current_artist_key and current_artist_key not in query_seed.lower():
                radio_queries.append(f"{current_artist_key} {query_seed}")

            broad_queries = [
                f"artists similar to {current_artist_key} best songs" if current_artist_key else f"songs like {query_seed}",
                f"mix similar to {query_seed}",
            ]

            artist_queries: list[str] = []
            if current_artist_key:
                artist_queries.extend([
                    f"artists like {current_artist_key}",
                    f"{current_artist_key} greatest hits",
                ])
        else:
            # Balanced: song-similarity and variety first, same artist is secondary
            radio_queries = [
                f"mix similar to {query_seed}",
                f"songs like {query_seed}",
            ]
            if current_artist_key and current_artist_key not in query_seed.lower():
                radio_queries.append(f"songs like {current_artist_key} {query_seed}")

            broad_queries = [
                f"{query_seed} radio",
                f"music recommendations similar to {query_seed}",
            ]
            if current_artist_key:
                broad_queries.append(f"artists similar to {current_artist_key} best songs")

            artist_queries = [
                f"{current_artist_key} best songs" if current_artist_key else "",
            ]
            artist_queries = [q for q in artist_queries if q]

        async def _safe_search(q: str, max_r: int) -> list[dict]:
            try:
                return await asyncio.wait_for(search_youtube(_normalize_search_query(q), max_results=max_r), timeout=8.0)
            except Exception:
                return []

        radio_results, broad_results, artist_results = await asyncio.gather(
            asyncio.gather(*[_safe_search(q, 12) for q in radio_queries]),
            asyncio.gather(*[_safe_search(q, 12) for q in broad_queries]),
            asyncio.gather(*[_safe_search(q, 12) for q in artist_queries]) if artist_queries else asyncio.gather(),
        )

        # GzVibe: radio (artist-focused) is high bias; Balanced: radio (song-focused) is high bias
        for q, results in zip(radio_queries, radio_results):
            _consider_candidates(
                results,
                source_bias=28 if state.autoplay_mode == "gzvibe" else 22,
                source_name=q,
            )
        for q, picks in zip(broad_queries, broad_results):
            _consider_candidates(
                picks,
                source_bias=16 if state.autoplay_mode == "gzvibe" else 14,
                source_name=q,
            )
        for q, picks in zip(artist_queries, artist_results):
            _consider_candidates(
                picks,
                # GzVibe: artist fallbacks are secondary; Balanced: penalise slightly to push variety
                source_bias=13 if state.autoplay_mode == "gzvibe" else 7,
                source_name=q,
            )
    except Exception as e:
        print(f"[Autoplay] Fallback search failed: {e}")

    if scored_candidates:
        return sorted(scored_candidates, key=lambda item: item[0], reverse=True)

    return []


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
        print(f"[Music] No target URL for {song.title}")
        return None
    try:
        import yt_dlp

        base_opts = dict(YTDL_STREAM_OPTS)
        cookiefile = _resolve_yt_cookiefile()
        if cookiefile:
            base_opts['cookiefile'] = cookiefile

        attempts = [
            base_opts,
            {**base_opts, 'format': 'bestaudio/best'},
            {k: v for k, v in base_opts.items() if k != 'format'},
        ]

        for opts in attempts:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=False)
                if not info:
                    continue

                url = info.get('url')
                if not url:
                    formats = info.get('formats') or []
                    audio_formats = [
                        f for f in formats
                        if f.get('url') and str(f.get('acodec', 'none')) != 'none'
                    ]
                    if audio_formats:
                        best_audio = max(audio_formats, key=lambda f: int(f.get('abr') or f.get('tbr') or 0))
                        url = best_audio.get('url')

                if isinstance(url, str) and url.strip() and url.startswith('http'):
                    print(f"[Music] Stream URL resolved for {song.title}: {url[:80]}...")
                    return url
            except Exception as e:
                print(f"[Music] yt-dlp stream resolve attempt failed for {song.title}: {e}")
    except Exception as e:
        print(f"[Music] yt-dlp stream resolve failed for {song.title}: {e}")

    # Fallback to original URL if it looks valid
    if song.url and isinstance(song.url, str) and song.url.startswith('http'):
        print(f"[Music] Using fallback URL for {song.title}")
        return song.url
    
    print(f"[Music] No valid stream URL available for {song.title}")
    return None

async def play_next(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    if not (state.queue and state.voice_client and state.voice_client.is_connected()):
        _remember_finished_song(state, state.current)
        _cleanup_song_file(state.current)
        state.current = None
        return

    state.current = state.queue.popleft()
    song = state.current
    retry_key = _song_identity(song) or (song.webpage_url or song.url or song.title).strip().lower()
    try:
        # Resolve stream URL in a thread with timeout so we never block the event loop
        try:
            stream_url = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _extract_stream_url(song)),
                timeout=20.0
            )
        except asyncio.TimeoutError:
            raise RuntimeError("Stream URL resolution timed out")
        
        if not stream_url:
            raise RuntimeError("No playable stream URL could be resolved")

        print(f"[Music] Stream URL ready, creating audio source...")
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
        state.source_transformer = source

        def after_play(error):
            try:
                state.source_transformer = None
                if error:
                    print(f'[Music] Player error on "{state.current.title if state.current else song.title}": {error}')
                    attempts = state.retry_attempts.get(retry_key, 0)
                    if attempts < 1:
                        state.retry_attempts[retry_key] = attempts + 1
                        print(f'[Music] Retrying track once after player error: {song.title}')
                        state.queue.appendleft(song)
                        state.current = None
                        asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)
                        return
                else:
                    print(f'[Music] Track finished: {state.current.title if state.current else song.title}')
                    state.retry_attempts.pop(retry_key, None)
                finished_song = state.current
                _remember_finished_song(state, finished_song)
                state.current = None
                _cleanup_song_file(finished_song)
                asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)
            except Exception as e:
                print(f'[Music] Error in after_play callback: {e}')

        print(f'[Music] Now playing: {song.title}')
        state.voice_client.play(source, after=after_play)
        print(f'[Music] Playback started successfully for: {song.title}')
    except Exception as e:
        print(
            f"[Music] Failed to create audio source for {song.title}: {e} | "
            f"FFMPEG_EXE={FFMPEG_EXE} | "
            f"Candidates={_ffmpeg_candidate_paths()}"
        )
        finished_song = state.current
        _remember_finished_song(state, finished_song)
        _cleanup_song_file(finished_song)
        state.current = None
        asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)

async def play_next_async(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    seed_song = state.current or state.last_finished
    # If queue is empty and autoplay is on, fetch a genuinely related song
    if not state.queue and state.autoplay and seed_song:
        print(f"[Autoplay] Attempting to queue related song for: {seed_song.title}")
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
                print(f"[Autoplay] Successfully queued: {r['title']} (mode={state.autoplay_mode})")
            else:
                print(f"[Autoplay] fetch_related_song returned None for: {seed_song.title}")
        except Exception as e:
            print(f"[Autoplay] Failed to queue related song: {e}")
    elif not state.queue and not state.autoplay and seed_song:
        print(f"[Autoplay] Queue empty and autoplay is disabled; no next song queued.")
    elif state.queue:
        print(f"[Autoplay] Queue not empty ({len(state.queue)} songs), will play next from queue")
    _loop = asyncio.get_running_loop()
    await play_next(guild_id, _loop)
    try:
        await _post_music_panel(guild_id)
    except Exception as e:
        print(f"[Music] Failed to update panel after starting playback: {e}")
    try:
        await _post_next_song_embed(guild_id)
    except Exception as e:
        print(f"[Music] Failed to post next song embed: {e}")


async def _post_next_song_embed(guild_id: int) -> None:
    """Post an automatic next-up embed in the music channel when playback advances."""
    state = get_music_state(guild_id)
    guild = client.get_guild(guild_id)
    if not guild or not state.current:
        return

    music_ch = _resolve_or_track_text_channel(guild, "music_channel", MUSIC_CHANNEL_NAME, "music-channel", "music")
    if not music_ch:
        return

    if state.queue:
        next_song = state.queue[0]
        embed = discord.Embed(title="⏭️ Up Next", color=0x3498DB)
        embed.description = f"[{next_song.title}]({next_song.webpage_url})"
        embed.add_field(name="Duration", value=next_song.format_duration(), inline=True)
        embed.add_field(name="Source", value="Queue", inline=True)
        await music_ch.send(embed=embed)
        return

    if not state.autoplay:
        return

    ranked = await _rank_autoplay_candidates(state, state.current)
    entry: dict | None = None
    score = 0
    source = ""
    using_fallback = False

    if ranked:
        score, entry, source = ranked[0]
    else:
        entry = await _autoplay_last_chance_pick(state, state.current)
        if not entry:
            return
        using_fallback = True
        source = "last-chance fallback"
        score = _autoplay_candidate_score(
            entry,
            current=state.current,
            recent_title_keys=state.recent_title_keys,
            recent_artist_keys=state.recent_artist_keys,
            queue_artist_keys=set(),
            current_artist_key=_song_artist_key(state.current),
            autoplay_mode=state.autoplay_mode,
            source_bias=9,
        )

    duration = int(entry.get("duration") or 0)
    m, s = divmod(duration, 60)
    h, m = divmod(m, 60)
    duration_text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    embed = discord.Embed(title="🔮 GzVibe Next", color=0x1DB954)
    embed.description = f"[{entry.get('title', 'Unknown title')}]({entry.get('webpage_url', '')})"
    embed.add_field(name="Duration", value=duration_text, inline=True)
    embed.add_field(name="Mode", value=_format_autoplay_mode(state.autoplay_mode), inline=True)
    embed.add_field(name="Rank Score", value=str(score), inline=True)
    footer = f"Predicted from: {source}"
    if using_fallback:
        footer += " • fallback"
    embed.set_footer(text=footer)
    await music_ch.send(embed=embed)

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

    @discord.ui.button(emoji="⏯️", label="Pause / Resume", style=discord.ButtonStyle.primary, row=0)
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
            await interaction.response.send_message("â–¶ï¸ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Skip Track", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Stop Session", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return
        state.queue.clear()
        state.current = None
        vc.stop()
        # Dismiss the deck since nothing is playing
        if state.now_playing_msg:
            try:
                await state.now_playing_msg.delete()
            except Exception:
                pass
            state.now_playing_msg = None
        await interaction.response.send_message("⏹️ Stopped and queue cleared.", ephemeral=True)

    @discord.ui.button(emoji="📋", label="Queue Snapshot", style=discord.ButtonStyle.secondary, row=1)
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
        embed = discord.Embed(title="📋 Queue Snapshot", description=desc, color=0x9B59B6)
        embed.set_footer(text="GzVibe Panel • Live Queue")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="🔉", label="Vol -10%", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.volume = max(0.0, round(state.volume - 0.1, 2))
        if state.source_transformer:
            state.source_transformer.volume = state.volume
        await interaction.response.send_message(
            f"🔉 Volume: {int(state.volume * 100)}%",
            ephemeral=True,
        )

    @discord.ui.button(emoji="🔊", label="Vol +10%", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.volume = min(1.0, round(state.volume + 0.1, 2))
        if state.source_transformer:
            state.source_transformer.volume = state.volume
        await interaction.response.send_message(
            f"🔊 Volume: {int(state.volume * 100)}%",
            ephemeral=True,
        )

    @discord.ui.button(emoji="🔁", label="Autoplay: OFF", style=discord.ButtonStyle.secondary, row=2, custom_id="autoplay_toggle")
    async def autoplay_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.autoplay = not state.autoplay
        button.label = f"Autoplay: {'ON' if state.autoplay else 'OFF'}"
        button.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)
        autoplay_note = (
            f"I'll queue related songs automatically in **{_format_autoplay_mode(state.autoplay_mode)}** mode!"
            if state.autoplay else ""
        )
        await interaction.followup.send(
            f"🔁 Autoplay is now **{'ON' if state.autoplay else 'OFF'}**. {autoplay_note}",
            ephemeral=True
        )

    @discord.ui.button(emoji="🎚️", label="Mode: GzVibe", style=discord.ButtonStyle.success, row=2, custom_id="autoplay_mode_toggle")
    async def autoplay_mode_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.autoplay_mode = "balanced" if state.autoplay_mode == "gzvibe" else "gzvibe"
        button.label = f"Mode: {_format_autoplay_mode(state.autoplay_mode)}"
        button.style = _autoplay_mode_button_style(state.autoplay_mode)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=_autoplay_mode_embed(state.autoplay_mode), ephemeral=True)

    @discord.ui.button(emoji="✨", label="Save To Playlist", style=discord.ButtonStyle.primary, row=2, custom_id="playlist_quick_add")
    async def playlist_quick_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        song = state.current or (state.queue[0] if state.queue else None)
        if not song:
            await interaction.response.send_message("No song is available to add right now.", ephemeral=True)
            return

        playlist_name = "GzVibe Favorites"
        user_playlists = _playlist_bucket(interaction.guild.id, interaction.user.id)
        tracks = user_playlists.setdefault(playlist_name, [])
        track = _playlist_track(song)

        if _playlist_has_track(tracks, track):
            embed = _gzvibe_playlist_embed(
                "🎵 Already In Playlist",
                f"[{song.title}]({song.webpage_url}) is already saved in **{playlist_name}**.",
                color=0xF39C12,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        tracks.append(track)
        _save_music_playlists()

        embed = _gzvibe_playlist_embed(
            "✅ Added To Playlist",
            (
                f"Saved [{song.title}]({song.webpage_url}) to **{playlist_name}**.\n"
                f"You now have `{len(tracks)}` songs in that playlist."
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


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


async def _post_music_panel(guild_id: int, force_new: bool = False):
    """Keep one persistent Now Playing panel and update it in place.
    Pass force_new=True when the user explicitly triggers /play so the panel
    moves to the bottom of the channel instead of editing in place."""
    state = get_music_state(guild_id)
    guild = client.get_guild(guild_id)
    if not guild:
        return
    music_ch = _resolve_or_track_text_channel(guild, "music_channel", MUSIC_CHANNEL_NAME, "music-channel", "music")
    if not music_ch:
        return
    if not state.current:
        return
    embed = discord.Embed(
        title="✦ GzVibe Control Deck ✦",
        description=(
            f"### [{state.current.title}]({state.current.webpage_url})\n"
            "`â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”`\n"
            f"**Vibe Mode:** `{_format_autoplay_mode(state.autoplay_mode)}`  •  "
            f"**Autoplay:** `{'ON' if state.autoplay else 'OFF'}`"
        ),
        color=0x00D1B2,
    )
    thumb = _youtube_thumbnail(state.current.webpage_url or state.current.url)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.add_field(name="⏱️ Duration", value=f"`{state.current.format_duration()}`", inline=True)
    embed.add_field(name="🎧 Requested By", value=state.current.requester.mention, inline=True)
    embed.add_field(name="🔊 Volume", value=f"`{int(state.volume * 100)}%`", inline=True)
    embed.add_field(name="📦 Queue Depth", value=f"`{len(state.queue)}` tracks waiting", inline=False)
    embed.add_field(name="🧩 Build", value=f"`{STARTUP_MARKER}`", inline=False)
    q = len(state.queue)
    footer_parts = []
    if q:
        footer_parts.append(f"{q} song{'s' if q != 1 else ''} in queue")
    if state.autoplay:
        footer_parts.append(f"🔁 Autoplay ON • {_format_autoplay_mode(state.autoplay_mode)}")
    if footer_parts:
        embed.set_footer(text="  ✦  ".join(footer_parts))
    else:
        embed.set_footer(text="GzVibe Deck • Controls Live")
    view = MusicControlView(guild_id)
    # Reflect autoplay state on the button
    for child in view.children:
        if getattr(child, "custom_id", None) == "autoplay_toggle":
            child.label = f"Autoplay: {'ON' if state.autoplay else 'OFF'}"
            child.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        if getattr(child, "custom_id", None) == "autoplay_mode_toggle":
            child.label = f"Mode: {_format_autoplay_mode(state.autoplay_mode)}"
            child.style = _autoplay_mode_button_style(state.autoplay_mode)

    # When force_new is requested (e.g. user ran /play), delete the old panel
    # so the fresh one lands at the bottom of the channel.
    if force_new and state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None

    # Prefer editing the existing panel to avoid flicker/disappearance.
    if state.now_playing_msg:
        try:
            await state.now_playing_msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            print(f"[Music] Could not edit existing music panel, sending a new one: {e}")
            state.now_playing_msg = None

    # Recover after restart: try to reuse the most recent bot panel message.
    if state.now_playing_msg is None:
        try:
            async for msg in music_ch.history(limit=20):
                if msg.author.id != client.user.id or not msg.embeds:
                    continue
                if (msg.embeds[0].title or "").strip() == "✦ GzVibe Control Deck ✦":
                    state.now_playing_msg = msg
                    break
        except Exception:
            pass

    if state.now_playing_msg:
        try:
            await state.now_playing_msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            print(f"[Music] Existing panel is not editable, posting a fresh panel: {e}")
            state.now_playing_msg = None

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

@client.tree.command(name="play", description="Search and play a song in your voice channel")
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
            vc = await member.voice.channel.connect(self_deaf=True)
            state.voice_client = vc
        elif vc.channel != member.voice.channel:
            await vc.move_to(member.voice.channel)
            state.voice_client = vc
        else:
            state.voice_client = vc

        try:
            results = await asyncio.wait_for(search_youtube_resilient(query, max_results=1), timeout=45.0)
        except (asyncio.TimeoutError, Exception) as _search_err:
            _is_timeout = isinstance(_search_err, asyncio.TimeoutError)
            _msg = (
                "Search timed out \u2014 YouTube may be rate-limiting. Try a direct YouTube URL."
                if _is_timeout else
                f"Search failed: {type(_search_err).__name__}: {_search_err}"
            )
            print(f'[Play] search error: {_search_err}')
            try:
                await interaction.followup.send(_msg)
            except Exception:
                pass
            return
        if not results:
            try:
                await interaction.followup.send(
                    "No playable results found right now. Try a more specific title (song + artist), "
                    "or paste a direct YouTube URL. If this keeps happening on Railway, add "
                    "YTDLP_COOKIES_PATH to a valid cookies.txt file and retry."
                )
            except Exception:
                pass
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
            _loop = asyncio.get_running_loop()
            await play_next(interaction.guild.id, _loop)
            try:
                await interaction.followup.send(f"▶️ Starting **{entry.title}** — see the player below!", ephemeral=True)
            except Exception:
                pass
            await _post_music_panel(interaction.guild.id, force_new=True)
        else:
            embed = discord.Embed(title="Added to Queue", description=f"[{entry.title}]({entry.webpage_url})", color=0x3498DB)
            embed.add_field(name="Position", value=len(state.queue))
            embed.add_field(name="Duration", value=entry.format_duration())
            try:
                await interaction.followup.send(embed=embed)
            except Exception:
                pass
            # Refresh deck in-place so Queue Depth updates without moving the panel
            await _post_music_panel(interaction.guild.id)
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[Play Error] {tb}')
        try:
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}")
        except Exception:
            pass

@client.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    vc.stop()
    await interaction.response.send_message("Skipped.")

@client.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    vc.pause()
    await interaction.response.send_message("Paused.")

@client.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        return
    vc.resume()
    await interaction.response.send_message("Resumed.")

@client.tree.command(name="stop", description="Stop playback and clear the queue")
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
    # Remove the control deck so it doesn't show stale song info
    if state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None
    await interaction.response.send_message("Stopped and queue cleared.")

@client.tree.command(name="leave", description="Disconnect the bot from the voice channel")
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
    # Remove the control deck on disconnect
    if state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None
    await interaction.response.send_message("Disconnected.")

@client.tree.command(name="queue", description="Show the current music queue")
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
    embed = discord.Embed(title="📋 GzVibe Queue", description=desc, color=0x9B59B6)
    if state.current:
        thumb = _youtube_thumbnail(state.current.webpage_url or state.current.url)
        if thumb:
            embed.set_thumbnail(url=thumb)
    embed.set_footer(text="Queue Snapshot • GzVibe")
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="nowplaying", description="Show what's currently playing")
async def nowplaying(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    state = get_music_state(interaction.guild.id)
    if not state.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    embed = discord.Embed(
        title="✦ GzVibe Live Track ✦",
        description=(
            f"## [{state.current.title}]({state.current.webpage_url})\n"
            "`â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”`"
        ),
        color=0x00D1B2,
    )
    thumb = _youtube_thumbnail(state.current.webpage_url or state.current.url)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.add_field(name="⏱️ Duration", value=f"`{state.current.format_duration()}`", inline=True)
    embed.add_field(name="🎧 Requested By", value=state.current.requester.mention, inline=True)
    embed.add_field(name="🔊 Volume", value=f"`{int(state.volume * 100)}%`", inline=True)
    embed.add_field(
        name="ðŸ” Autoplay",
        value=f"`{'ON' if state.autoplay else 'OFF'}` in `{_format_autoplay_mode(state.autoplay_mode)}`",
        inline=False,
    )
    embed.add_field(name="🧩 Build", value=f"`{STARTUP_MARKER}`", inline=False)
    embed.set_footer(text="GzVibe Live View • Neon Style")
    await interaction.response.send_message(embed=embed)
    await _post_music_panel(interaction.guild.id, force_new=True)

@client.tree.command(name="volume", description="Set the playback volume (0-100)")
async def volume(interaction: discord.Interaction, level: int):
    if not await _require_music_channel(interaction):
        return
    if not 0 <= level <= 100:
        await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    state.volume = level / 100
    await interaction.response.send_message(f"Volume set to {level}% (applies immediately to next track).")


@client.tree.command(name="autoplaymode", description="Set how strict autoplay should be")
@discord.app_commands.choices(mode=[
    discord.app_commands.Choice(name="GzVibe", value="gzvibe"),
    discord.app_commands.Choice(name="Balanced", value="balanced"),
])
async def autoplaymode(interaction: discord.Interaction, mode: str):
    if not await _require_music_channel(interaction):
        return
    state = get_music_state(interaction.guild.id)
    state.autoplay_mode = mode if mode in {"gzvibe", "balanced"} else "gzvibe"
    await interaction.response.send_message(embed=_autoplay_mode_embed(state.autoplay_mode), ephemeral=True)
    await _post_music_panel(interaction.guild.id)


@client.tree.command(name="autoplaydebug", description="Preview the top autoplay candidates")
async def autoplaydebug(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return
    state = get_music_state(interaction.guild.id)
    seed_song = state.current or state.last_finished
    if not seed_song:
        await interaction.response.send_message("Play a song first so autoplay has a seed track.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    ranked = await _rank_autoplay_candidates(state, seed_song)
    state.last_autoplay_debug = [
        _summarize_autoplay_debug({
            **entry,
            "source": source_name,
            "score": score,
            "artist": _entry_artist_key(entry) or "Unknown",
        })
        for score, entry, source_name in ranked[:5]
    ]

    if not state.last_autoplay_debug:
        await interaction.followup.send("I couldn't find any autoplay candidates right now.", ephemeral=True)
        return

    lines = []
    for idx, item in enumerate(state.last_autoplay_debug, 1):
        lines.append(
            f"`{idx}.` **{item['score']}** • {item['title']}\n"
            f"Artist: {item['artist']} • Duration: {item['duration']} • Source: {item['source']}"
        )

    embed = discord.Embed(
        title="Autoplay Debug",
        description="\n\n".join(lines),
        color=0x1DB954,
    )
    embed.add_field(name="Seed Song", value=f"[{seed_song.title}]({seed_song.webpage_url})", inline=False)
    embed.add_field(name="Mode", value=_format_autoplay_mode(state.autoplay_mode), inline=True)
    embed.add_field(name="Autoplay", value="ON" if state.autoplay else "OFF", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@client.tree.command(name="nextsong", description="Show what the bot will play next")
async def nextsong(interaction: discord.Interaction):
    if not await _require_music_channel(interaction):
        return

    state = get_music_state(interaction.guild.id)
    if state.queue:
        upcoming = state.queue[0]
        embed = discord.Embed(title="⏭️ Next Song", color=0x3498DB)
        embed.description = f"[{upcoming.title}]({upcoming.webpage_url})"
        embed.add_field(name="Duration", value=upcoming.format_duration(), inline=True)
        embed.add_field(name="From", value="Queue", inline=True)
        await interaction.response.send_message(embed=embed)
        return

    seed_song = state.current or state.last_finished
    if not state.autoplay or not seed_song:
        await interaction.response.send_message(
            "Nothing queued right now. Turn on autoplay or add songs with `/play`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    ranked = await _rank_autoplay_candidates(state, seed_song)
    if not ranked:
        await interaction.followup.send("I couldn't predict a next autoplay song right now.", ephemeral=True)
        return

    score, entry, source = ranked[0]
    duration = int(entry.get("duration") or 0)
    m, s = divmod(duration, 60)
    h, m = divmod(m, 60)
    duration_text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    embed = discord.Embed(title="🔮 Predicted Next Song", color=0x1DB954)
    embed.description = f"[{entry.get('title', 'Unknown title')}]({entry.get('webpage_url', '')})"
    embed.add_field(name="Duration", value=duration_text, inline=True)
    embed.add_field(name="Source", value=source, inline=True)
    embed.add_field(name="Score", value=str(score), inline=True)
    embed.set_footer(text=f"Autoplay mode: {_format_autoplay_mode(state.autoplay_mode)}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@client.tree.command(name="playlist", description="Manage your saved music playlists")
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Save current queue", value="save"),
    discord.app_commands.Choice(name="List playlists", value="list"),
    discord.app_commands.Choice(name="Play a playlist", value="play"),
    discord.app_commands.Choice(name="Delete a playlist", value="delete"),
])
@discord.app_commands.describe(
    action="What to do",
    name="Playlist name (required for save/play/delete)",
)
async def playlist(interaction: discord.Interaction, action: str, name: str = ""):
    if not await _require_music_channel(interaction):
        return

    state = get_music_state(interaction.guild.id)
    user_playlists = _playlist_bucket(interaction.guild.id, interaction.user.id)
    normalized_name = _normalize_playlist_name(name)

    if action == "list":
        if not user_playlists:
            await interaction.response.send_message("You don't have any saved playlists yet.", ephemeral=True)
            return
        lines = []
        for pname, tracks in sorted(user_playlists.items()):
            lines.append(f"• **{pname}** — `{len(tracks)}` songs")
        embed = discord.Embed(title="🎶 Your Playlists", description="\n".join(lines), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if not normalized_name:
        await interaction.response.send_message("Please provide a playlist name.", ephemeral=True)
        return

    if action == "save":
        tracks: list[dict] = []
        if state.current:
            tracks.append(_playlist_track(state.current))
        tracks.extend(_playlist_track(song) for song in list(state.queue))
        if not tracks:
            await interaction.response.send_message("Nothing is playing or queued to save.", ephemeral=True)
            return
        if len(tracks) > 50:
            tracks = tracks[:50]
        user_playlists[normalized_name] = tracks
        _save_music_playlists()
        await interaction.response.send_message(
            f"Saved **{normalized_name}** with `{len(tracks)}` songs.",
            ephemeral=True,
        )
        return

    if action == "delete":
        if normalized_name not in user_playlists:
            await interaction.response.send_message("That playlist doesn't exist.", ephemeral=True)
            return
        del user_playlists[normalized_name]
        _save_music_playlists()
        await interaction.response.send_message(f"Deleted playlist **{normalized_name}**.", ephemeral=True)
        return

    if action == "play":
        tracks = user_playlists.get(normalized_name)
        if not tracks:
            await interaction.response.send_message("That playlist doesn't exist or is empty.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
            return

        await interaction.response.defer()
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await member.voice.channel.connect(self_deaf=True)
            state.voice_client = vc
        elif vc.channel != member.voice.channel:
            await vc.move_to(member.voice.channel)
            state.voice_client = vc
        else:
            state.voice_client = vc

        added = 0
        for track in tracks[:50]:
            lookup = track.get("webpage_url") or track.get("title")
            if not lookup:
                continue
            try:
                results = await search_youtube_resilient(lookup, max_results=1)
                if not results:
                    continue
                r = results[0]
                state.queue.append(SongEntry(
                    title=r.get("title", track.get("title", "Unknown")),
                    url=r["url"],
                    webpage_url=r.get("webpage_url", track.get("webpage_url", "")),
                    duration=r.get("duration", track.get("duration", 0)),
                    requester=interaction.user,
                    local_path=r.get("local_path"),
                ))
                added += 1
            except Exception as e:
                print(f"[Playlist] Could not queue track from {normalized_name}: {e}")

        if added == 0:
            await interaction.followup.send("Could not queue any songs from that playlist right now.", ephemeral=True)
            return

        if vc and not vc.is_playing() and not vc.is_paused() and state.queue:
            await play_next(interaction.guild.id, asyncio.get_running_loop())
            await _post_music_panel(interaction.guild.id, force_new=True)

        await interaction.followup.send(
            f"Queued `{added}` songs from **{normalized_name}**.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Unknown playlist action.", ephemeral=True)

@client.tree.command(name="announce", description="Send an announcement to a specific channel")
@app_commands.default_permissions(manage_messages=True)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You don't have permission to make announcements.", ephemeral=True)
        return
    await channel.send(message)
    await interaction.response.send_message(f"Announcement sent to {channel.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "announce", f"Channel: {channel.mention}")

# ── Gaming Community Commands ─────────────────────────────────────────────────

@client.tree.command(name="lfg", description="Post a Looking For Group message")
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


@client.tree.command(name="lfgfilter", description="Browse active LFG posts with filters")
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


@client.tree.command(name="gamertag", description="Set your gamertag for a platform")
async def gamertag_set(interaction: discord.Interaction, platform: str, tag: str):
    platforms = ["PC", "Xbox", "PlayStation", "Nintendo", "Steam", "Epic"]
    platform_clean = platform.strip()
    GAMERTAGS.setdefault(interaction.user.id, {})[platform_clean] = tag
    embed = discord.Embed(title="🎮 Gamertag Saved", color=0x00FF7F)
    embed.add_field(name="Platform", value=platform_clean, inline=True)
    embed.add_field(name="Tag", value=tag, inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="gamertags", description="View a player's gamertags")
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


@client.tree.command(name="personalspace", description="Set up the Personal Space system — creates a lobby VC that spawns private rooms (Admin only)")
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
                "1ï¸âƒ£ Join **➕ Join to Create** to get your own private voice room\n"
                "2ï¸âƒ£ Your room is named after you and only you can see it by default\n"
                "3ï¸âƒ£ Invite friends by right-clicking the channel → Edit → Permissions\n"
                "4ï¸âƒ£ Room auto-deletes when everyone leaves\n\n"
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
        await interaction.followup.send(f"âŒ Failed to create lobby: {e}", ephemeral=True)

@client.tree.command(name="setupgames", description="Create game roles, channels, and the game selector (Admin only)")
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
def _gz_progress_bar(current: int, needed: int, width: int = 16) -> str:
    if needed <= 0:
        return "█" * width
    ratio = max(0.0, min(1.0, current / needed))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _gz_brand_image_url(guild: discord.Guild | None) -> str | None:
    if guild is None:
        return None
    if guild.banner:
        return guild.banner.url
    if guild.icon:
        return guild.icon.url
    return None


def _build_gz_levelup_card(
    guild: discord.Guild,
    member: discord.Member,
    new_level: int,
    total_xp: int,
    xp_in_level: int,
    xp_for_next: int,
) -> discord.Embed:
    progress_bar = _gz_progress_bar(max(0, xp_in_level), max(1, xp_for_next))
    embed = discord.Embed(
        title="Gaming Zone | Level Up",
        description=f"You reached **Level {new_level}**.",
        color=0x00C853,
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(new_level), inline=True)
    embed.add_field(name="Total XP", value=f"{total_xp:,}", inline=True)
    embed.add_field(
        name=f"Progress to Level {new_level + 1}",
        value=f"{progress_bar}  {max(0, xp_in_level)}/{max(1, xp_for_next)} XP",
        inline=False,
    )
    brand_image = _gz_brand_image_url(guild)
    if brand_image:
        embed.set_image(url=brand_image)
    embed.set_footer(text="Gaming Zone Level Card")
    embed.timestamp = discord.utils.utcnow()
    return embed


def _build_gz_rank_card(
    guild: discord.Guild,
    member: discord.Member,
    level: int,
    total_xp: int,
    xp_in_level: int,
    xp_for_next: int,
    voice_mins: int,
) -> discord.Embed:
    progress_bar = _gz_progress_bar(max(0, xp_in_level), max(1, xp_for_next))
    progress_pct = int(round((max(0, xp_in_level) / max(1, xp_for_next)) * 100))
    embed = discord.Embed(
        title="Gaming Zone | Rank Card",
        description=f"**{member.mention}** progression snapshot",
        color=0x5865F2,
    )
    embed.set_author(name=f"{member.display_name} • Gaming Zone Rank Card", icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=f"{level}", inline=True)
    embed.add_field(name="Total XP", value=f"{total_xp:,}", inline=True)
    embed.add_field(name="Voice Time", value=f"{voice_mins:,} min", inline=True)
    embed.add_field(
        name=f"Progress to Level {level + 1}",
        value=f"{progress_bar}  {max(0, xp_in_level)}/{max(1, xp_for_next)} XP  ({progress_pct}%)",
        inline=False,
    )
    brand_image = _gz_brand_image_url(guild)
    if brand_image:
        embed.set_image(url=brand_image)
    embed.set_footer(text="Gaming Zone Rank Card")
    embed.timestamp = discord.utils.utcnow()
    return embed


@client.tree.command(name="rank", description="Check your text level and XP")
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
    voice_mins = VOICE_MINUTES.get(gid, {}).get(target.id, 0)
    embed = _build_gz_rank_card(
        interaction.guild,
        target,
        level,
        xp,
        xp_in_level,
        xp_for_next,
        voice_mins,
    )
    await interaction.response.send_message(embed=embed)

def _collect_leaderboard_top(guild: discord.Guild, limit: int = 20) -> list[tuple[int, int]]:
    gid = guild.id
    guild_xp = XP_DATA.get(gid, {})
    ranking: list[tuple[int, int, str]] = []
    for member in guild.members:
        xp = int(guild_xp.get(member.id, 0))
        ranking.append((member.id, xp, member.display_name.casefold()))
    ranking.sort(key=lambda row: (-row[1], row[2]))
    return [(uid, xp) for uid, xp, _ in ranking[:max(1, limit)]]


def _build_xp_leaderboard_embed(guild: discord.Guild, top: list[tuple[int, int]]) -> discord.Embed:
    embed = discord.Embed(title="🏆 GamingZone Leaderboard", color=0xF1C40F)
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, xp) in enumerate(top):
        member = guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        lvl = _xp_to_level(xp)
        prefix = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{prefix} **{name}** — Level {lvl} ({xp} XP)")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Live XP standings • Updated")
    embed.timestamp = discord.utils.utcnow()
    return embed


@client.tree.command(name="leaderboard", description="Show the top 20 most active members")
async def leaderboard(interaction: discord.Interaction):
    top = _collect_leaderboard_top(interaction.guild, limit=20)
    embed = _build_xp_leaderboard_embed(interaction.guild, top)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="liveleaderboard", description="Show GamingZone live XP leaderboard")
async def liveleaderboard(interaction: discord.Interaction):
    top = _collect_leaderboard_top(interaction.guild, limit=20)
    embed = _build_xp_leaderboard_embed(interaction.guild, top)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="restorexpsnapshot", description="Restore saved XP snapshot for known members (Admin only)")
@app_commands.default_permissions(administrator=True)
async def restorexpsnapshot(interaction: discord.Interaction, force: bool = False):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    restored, missing = _restore_xp_snapshot_to_guild(interaction.guild, force=force)
    await interaction.response.send_message(
        f"XP snapshot restore complete. Restored: **{restored}** | Missing name matches: **{missing}**.",
        ephemeral=True,
    )


@client.tree.command(name="prestige", description="View and manage your prestige level (Phase 3 retention)")
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


@client.tree.command(name="prestigereset", description="Reset your level to gain prestige (costs PokeCoins)")
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
            f"âŒ Not enough PokeCoins. You have **{coins}**, need **{PRESTIGE_RESET_COST}**.",
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


@client.tree.command(name="cosmetics", description="View and purchase cosmetic upgrades")
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
            affordable = "✅" if coins >= cos["cost"] else "âŒ"
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


@client.tree.command(name="cosmeticsbuy", description="Purchase a cosmetic upgrade")
@app_commands.describe(cosmetic_id="The cosmetic ID (e.g., title_badge, border_glow)")
async def cosmeticsbuy(interaction: discord.Interaction, cosmetic_id: str):
    from pokemon_game import WALLETS, _wallet
    
    uid = interaction.user.id
    cos_id = cosmetic_id.lower().strip()

    if cos_id not in COSMETICS:
        await interaction.response.send_message(
            f"âŒ Unknown cosmetic `{cos_id}`. Use `/cosmetics shop` to see available items.",
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
            f"âŒ Not enough PokeCoins. You have **{coins}**, need **{cos['cost']}**.",
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
@client.tree.command(name="invites", description="Check how many members you or someone else has invited")
async def invites(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    count = INVITE_COUNTS.get(interaction.guild.id, {}).get(target.id, 0)
    await interaction.response.send_message(
        f"📨 **{target.display_name}** has invited **{count}** member(s) to the server."
    )

# ── Ticket Commands ───────────────────────────────────────────────────────────
@client.tree.command(name="setupticketchannel", description="Post the ticket panel in a channel (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setupticketchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    embed = discord.Embed(
        title="🎫 Gaming Zone Support Desk",
        description=(
            "Need help with bot commands, roles, or server issues?\n"
            "Press the button below to open your private support ticket."
        ),
        color=0x5865F2,
    )
    embed.add_field(name="What to include", value="Issue summary, screenshots, and what you already tried.", inline=False)
    embed.add_field(name="Response flow", value="Staff reviews in order and replies in your ticket channel.", inline=False)
    embed.set_footer(text="One open ticket per member • Gaming Zone")
    await channel.send(embed=embed, view=OpenTicketView())
    await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "setupticketchannel", f"Panel posted in {channel.mention}")

# ── Ticket Admin Commands ─────────────────────────────────────────────────────
@client.tree.command(name="ticketlist", description="List all currently open tickets (Admin only)")
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


@client.tree.command(name="closeticket", description="Force-close a ticket channel (Admin only)")
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


@client.tree.command(name="addticketstaff", description="Give a role access to all ticket channels (Admin only)")
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


@client.tree.command(name="nukechannel", description="Delete all messages in a channel (Admin only)")
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


@client.tree.command(name="setupchat", description="Post a styled embed in a channel - rules, welcome, info (Admin only)")
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


@client.tree.command(name="movechannel", description="Move a channel to a different category (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel to move", category="Exact name of the destination category")
async def cmd_movechannel(interaction: discord.Interaction, channel: discord.TextChannel, category: str):
    await movechannel(interaction, channel, category)

# ── Reaction Role Commands ────────────────────────────────────────────────────
@client.tree.command(name="reactionrole", description="Add a reaction role to a message (Admin only)")
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
@client.tree.command(name="giveaway", description="Start a giveaway (Admin only)")
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

@client.tree.command(name="endgiveaway", description="End a giveaway early (Admin only)")
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
@client.tree.command(name="addstreamer", description="Add a streamer to follow (Admin only)")
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

@client.tree.command(name="removestreamer", description="Stop following a streamer (Admin only)")
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

@client.tree.command(name="streamers", description="List all followed streamers")
async def liststreamers(interaction: discord.Interaction):
    if not STREAMERS:
        await interaction.response.send_message("No streamers are being followed yet. Use `/addstreamer`.", ephemeral=True)
        return
    embed = discord.Embed(title="📡 Followed Streamers", color=0x9146FF)
    lines = [f"{'🔴' if s['last_live'] else '⚫'} **{s['name']}** ({s['platform'].title()})" for s in STREAMERS]
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="serverhealth", description="Show bot/server health diagnostics (Admin only)")
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
        f"giveaway_check: {'✅' if giveaway_check.is_running() else 'âŒ'}",
        f"streamer_check: {'✅' if streamer_check.is_running() else 'âŒ'}",
        f"free_games_check: {'✅' if free_games_check.is_running() else 'âŒ'}",
        f"empty_vc_cleanup: {'✅' if empty_vc_cleanup.is_running() else 'âŒ'}",
        f"ticket_sla_check: {'✅' if ticket_sla_check.is_running() else 'âŒ'}",
    ]

    wiring_values = [mod_log, ticket_log, casino_ch, pokemon_ch, music_ch, verify_ch, free_games_ch]
    wiring_ok = sum(1 for item in wiring_values if item)
    tasks_running = sum(
        1
        for is_running in (
            giveaway_check.is_running(),
            streamer_check.is_running(),
            free_games_check.is_running(),
            empty_vc_cleanup.is_running(),
            ticket_sla_check.is_running(),
        )
        if is_running
    )
    health_score = wiring_ok + tasks_running
    score_total = len(wiring_values) + 5
    score_ratio = (health_score / score_total) if score_total else 0.0

    def _meter(ratio: float, width: int = 10) -> str:
        ratio = max(0.0, min(1.0, ratio))
        fill = int(round(ratio * width))
        return "█" * fill + "░" * (width - fill)

    def _mix(c1: int, c2: int, t: float) -> int:
        t = max(0.0, min(1.0, t))
        r1, g1, b1 = (c1 >> 16) & 0xFF, (c1 >> 8) & 0xFF, c1 & 0xFF
        r2, g2, b2 = (c2 >> 16) & 0xFF, (c2 >> 8) & 0xFF, c2 & 0xFF
        r = int(round(r1 + (r2 - r1) * t))
        g = int(round(g1 + (g2 - g1) * t))
        b = int(round(b1 + (b2 - b1) * t))
        return (r << 16) | (g << 8) | b

    if score_ratio < 0.5:
        health_color = _mix(0xE74C3C, 0xF1C40F, score_ratio / 0.5)
        health_state = "Recovery Mode"
        health_badge = "🔴"
    elif score_ratio < 0.85:
        health_color = _mix(0xF1C40F, 0x2ECC71, (score_ratio - 0.5) / 0.35)
        health_state = "Degraded"
        health_badge = "🟡"
    else:
        health_color = 0x2ECC71
        health_state = "Online"
        health_badge = "🟢"

    uptime_seconds = int((now - BOT_BOOT_TIME_UTC).total_seconds())
    uptime_confidence = min(1.0, uptime_seconds / 86400.0)
    task_ratio = tasks_running / 5.0
    wiring_ratio = wiring_ok / float(len(wiring_values)) if wiring_values else 0.0
    if tracked_ticket_channels:
        ticket_health_ratio = max(0.0, 1.0 - (stale_tickets / float(len(tracked_ticket_channels))))
    else:
        ticket_health_ratio = 1.0

    member_count = guild.member_count or len(guild.members)
    online_count = sum(1 for m in guild.members if m.status != discord.Status.offline)
    new_joins_today = sum(
        1
        for m in guild.members
        if m.joined_at and (now - m.joined_at).total_seconds() <= 86400
    )
    guild_cmds = len(client.tree.get_commands(guild=guild))

    status_badges = [
        f"{health_badge} {health_state}",
        f"ðŸ§  Cmds {guild_cmds}",
        f"🎫 Ticket {'✓' if ticket_log else '✗'}",
        f"🎬 Stream {'✓' if _resolve_streamer_channel(guild) else '✗'}",
        f"🎮 Free {'✓' if free_games_ch else '✗'}",
    ]

    alerts = []
    if not ticket_log:
        alerts.append("Ticket log is not linked")
    if tasks_running < 5:
        alerts.append(f"Only {tasks_running}/5 background tasks running")
    if stale_tickets > 0:
        alerts.append(f"{stale_tickets} ticket(s) over SLA")

    embed = discord.Embed(title="🩺 Gaming Zone Ops Dashboard", color=health_color)
    embed.description = (
        f"**{health_badge} {health_state}**  •  System Score **{health_score}/{score_total}**\n"
        + " | ".join(status_badges)
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_author(name=f"{guild.name} Live Telemetry", icon_url=(guild.icon.url if guild.icon else discord.Embed.Empty))

    if alerts:
        embed.add_field(name="âš ï¸ Attention", value="\n".join(f"• {a}" for a in alerts[:2]), inline=False)

    embed.add_field(
        name="âš™ï¸ Runtime Core",
        value=(
            f"Uptime: **{uptime}**\n"
            f"Confidence: `{_meter(uptime_confidence)}` {int(uptime_confidence * 100)}%\n"
            f"PID: `{os.getpid()}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="🧭 Channel Grid",
        value=(
            f"mod-log {'✅' if mod_log else 'âŒ'}\n"
            f"ticket-log {'✅' if ticket_log else 'âŒ'}\n"
            f"casino {'✅' if casino_ch else 'âŒ'}\n"
            f"pokemon {'✅' if pokemon_ch else 'âŒ'}\n"
            f"music {'✅' if music_ch else 'âŒ'}\n"
            f"verify {'✅' if verify_ch else 'âŒ'}\n"
            f"free-games {'✅' if free_games_ch else 'âŒ'}\n"
            f"Wiring: `{_meter(wiring_ratio)}` {int(wiring_ratio * 100)}%"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎫 Ticket Ops",
        value=(
            f"Active: **{len(tracked_ticket_channels)}**\n"
            f"Over SLA: **{stale_tickets}**\n"
            f"SLA: `{_meter(ticket_health_ratio)}` {int(ticket_health_ratio * 100)}%"
        ),
        inline=True,
    )
    embed.add_field(name="ðŸ” Task Grid", value="\n".join(task_lines) + f"\nTask Health: `{_meter(task_ratio)}` {int(task_ratio * 100)}%", inline=False)
    embed.add_field(
        name="📊 Guild Snapshot",
        value=(
            f"Members: **{member_count}**\n"
            f"Online: **{online_count}**\n"
            f"New joins (24h): **{new_joins_today}**\n"
            f"Boost tier: **{guild.premium_tier}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎮 Game Systems",
        value=(
            f"Streamers tracked: **{len(STREAMERS)}**\n"
            f"Giveaways active: **{len([g for g in GIVEAWAYS.values() if not g.get('ended')])}**\n"
            f"Posted free-game IDs: **{len(POSTED_FREE_GAMES)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎵 Music Engine",
        value=(
            f"Channel linked: **{'Yes' if music_ch else 'No'}**\n"
            f"Voice client active: **{'Yes' if guild.voice_client else 'No'}**\n"
            f"Managed channel IDs: **{len(MANAGED_CHANNEL_IDS.get(str(guild.id), {}))}**"
        ),
        inline=True,
    )
    embed.set_footer(text="Gaming Zone Ops Panel • Live Telemetry • Region europe-west4")
    embed.timestamp = now
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="weeklyrecap", description="Show a weekly community recap")
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
    embed.add_field(name="ðŸ† Top XP", value="\n".join(xp_lines), inline=False)
    embed.add_field(name="ðŸŽ™ï¸ Top Voice Time", value="\n".join(voice_lines), inline=False)
    embed.set_footer(text="Tip: run /serverhealth for live diagnostics")
    embed.timestamp = now
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="bot", description="About this bot and what it can do (Admin/Mod only)")
@app_commands.default_permissions(administrator=True)
async def bot_info(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.moderate_members):
        await interaction.response.send_message("🔒 This command is restricted to Admins and Moderators.", ephemeral=True)
        return
    
    # Page 1: Music & Gaming
    embed1 = discord.Embed(
        title="🤖 About This Bot — Page 1/4",
        description="A fully-featured gaming community bot. Use â–¶ï¸ to see more.",
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
        "Level ups are sent privately as branded cards. Use `/rank` and `/leaderboard`."
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
    embed4.add_field(name="ðŸ·ï¸ Reaction Roles & Free Games", value=(
        "ðŸ·ï¸ Admins add reaction roles to messages with `/reactionrole`.\n"
        "🎮 Auto-posts 100% off Steam games to **#free-games** every 4 hours."
    ), inline=False)
    embed4.add_field(name="📨 Tracking & Safety", value=(
        "Invite tracking showing who invited each member.\n"
        "ðŸ›¡ï¸ Auto-moderation, word filter, link quarantine, and comprehensive logging."
    ), inline=False)
    embed4.set_footer(text="Use /help for user commands • Use /adminhelp for mod commands")
    
    embeds = [embed1, embed2, embed3, embed4]
    view = PaginatedHelpView(embeds, interaction.user.id)
    await interaction.response.send_message(embed=embeds[0], view=view, ephemeral=True)

@client.tree.command(name="help", description="Show all available bot commands (Admin/Mod only)")
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
    embed1.add_field(name="â„¹ï¸ Navigation", value="Use â—€ï¸ â–¶ï¸ to flip through pages", inline=False)
    
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


@client.tree.command(name="quickstart", description="New player guide — Get started in 30 seconds")
async def quickstart(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🚀 Welcome to the Gaming Zone!",
        description="Follow these 4 quick steps to get started.",
        color=0x2ECC71
    )

    embed.add_field(name="Step 1ï¸âƒ£: Verify", value=(
        f"Go to **#{VERIFY_CHANNEL_NAME}** and click **Verify — Get Access**.\n"
        "This unlocks your member channels and game features."
    ), inline=False)

    embed.add_field(name="Step 2ï¸âƒ£: Save Your Gamertag", value=(
        "Run `/gamertag <platform> <username>`\n"
        "Example: `/gamertag ps5 MyPlayName`"
    ), inline=False)

    embed.add_field(name="Step 3ï¸âƒ£: Find a Squad", value=(
        "Post an LFG: `/lfg valorant 3 looking for comp`\n"
        "Browse posts: `/lfgfilter game:valorant`\n"
        "Join using the **Going** button"
    ), inline=False)

    embed.add_field(name="Step 4ï¸âƒ£: Earn XP & Level Up", value=(
        "💬 Chat = 15 XP/min (with cooldown)\n"
        "ðŸŽ™ï¸ Voice = 1 XP/min\n"
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


@client.tree.command(name="adminhelp", description="Admin/Mod command guide (Admin only)")
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


@client.tree.command(name="setupfreegames", description="Create #free-games and post current Steam deals now (Admin only)")
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
        f"{'✅ Posted **' + str(len(games_to_post)) + '** new free game(s) with images and claim buttons.' if games_to_post else 'âš ï¸ No new free games found right now.'} The channel auto-updates every 4 hours.",
        ephemeral=True,
    )
    await _log_admin_cmd(interaction, "setupfreegames", f"Channel: {ch.mention} | Games posted: {len(games_to_post)}")


@client.tree.command(name="setupchannels", description="Create the casino, Pokémon battle, and music channels (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setupchannels(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    await _ensure_log_channels(guild)
    await _ensure_feature_channels(guild)
    await _ensure_social_alert_channel(guild)
    await _post_verify_embed(guild)
    all_chs = [
        discord.utils.get(guild.text_channels, name=n)
        for n in (GAMBLING_CHANNEL_NAME, POKEMON_CHANNEL_NAME, MUSIC_CHANNEL_NAME, STREAMER_CHANNEL_NAME)
    ]
    mentions = ", ".join(ch.mention for ch in all_chs if ch)
    await interaction.followup.send(
        f"✅ Channels set up with **@{GAMER_ROLE_NAME}**-only access: {mentions}\n"
        f"**#{VERIFY_CHANNEL_NAME}** created — visible to new members, hidden after they verify.\n"
        f"Members can post stream links in **#{STREAMER_CHANNEL_NAME}** without auto-mod ad penalties.",
        ephemeral=True,
    )
    await _log_admin_cmd(interaction, "setupchannels", f"Channels: {mentions}")


@client.tree.command(name="setupidlerpg", description="Apply Gamer-only access to the IdleRPG channel ID (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setupidlerpg(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    idlerpg_ch = await _ensure_idlerpg_channel_access(guild)
    if idlerpg_ch is None:
        msg = f"âš ï¸ IdleRPG channel ID {IDLERPG_CHAT_CHANNEL_ID} was not found in this server."
        await interaction.followup.send(msg, ephemeral=True)
        await _log_admin_cmd(interaction, "setupidlerpg", msg)
        return

    msg = (
        f"✅ Applied **@{GAMER_ROLE_NAME}**-only access to {idlerpg_ch.mention} "
        f"(ID: `{IDLERPG_CHAT_CHANNEL_ID}`)."
    )
    await interaction.followup.send(msg, ephemeral=True)
    await _log_admin_cmd(interaction, "setupidlerpg", f"Channel: {idlerpg_ch.mention} ({IDLERPG_CHAT_CHANNEL_ID})")

    # Also ensure the pinned starter panel exists so new players see it immediately.
    panel_msg, panel_status = await _ensure_idlerpg_starter_panel(idlerpg_ch)
    if panel_msg is not None:
        await interaction.followup.send(
            f"Starter panel {panel_status} in {idlerpg_ch.mention}.",
            ephemeral=True,
        )

    status_msg, status_state = await _ensure_idlerpg_status_panel(idlerpg_ch)
    if status_msg is not None:
        await interaction.followup.send(
            f"Status panel {status_state} in {idlerpg_ch.mention}.",
            ephemeral=True,
        )


@client.tree.command(name="setupidlerpgpanel", description="Post the persistent IdleRPG starter panel (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setupidlerpgpanel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    panel_ch = guild.get_channel(IDLERPG_CHAT_CHANNEL_ID)
    if not isinstance(panel_ch, discord.TextChannel):
        await interaction.followup.send(f"IdleRPG channel ID `{IDLERPG_CHAT_CHANNEL_ID}` was not found.", ephemeral=True)
        return

    panel_msg, panel_status = await _ensure_idlerpg_starter_panel(panel_ch)
    if panel_msg is None:
        await interaction.followup.send(f"Could not create starter panel in {panel_ch.mention}.", ephemeral=True)
        return

    await interaction.followup.send(f"IdleRPG starter panel {panel_status} in {panel_ch.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "setupidlerpgpanel", f"Starter panel {panel_status} in {panel_ch.mention}")


async def _ensure_idlerpg_starter_panel(panel_ch: discord.TextChannel) -> tuple[discord.Message | None, str]:
    # Reuse existing pinned starter panel if already present to avoid duplicate pinned posts.
    try:
        pinned = await panel_ch.pins()
        for p in pinned:
            if p.author and p.author.id == client.user.id and p.embeds:
                if (p.embeds[0].title or "").strip().lower() == "idlerpg starter panel":
                    return p, "already exists"
    except Exception:
        pass

    embed = discord.Embed(
        title="IdleRPG Starter Panel",
        description=(
            "Use the buttons below to progress without typing commands constantly.\n"
            "Character Setup -> Adventure Hub -> Check Status -> Reward decisions"
        ),
        color=0x5865F2,
    )
    embed.add_field(name="Timed Missions", value="Adventures are timed. Use Status to resolve rewards.", inline=False)
    embed.add_field(name="Gear Loop", value="After status resolves a drop, use equip/sell/keep buttons.", inline=False)
    embed.add_field(name="Store", value="Open the class crate store from the panel buttons.", inline=False)
    embed.add_field(name="Codex", value="Use /idlerpgcodex for a clickable full game index.", inline=False)
    embed.set_footer(text="IdleRPG panel • persistent controls")

    try:
        msg = await panel_ch.send(embed=embed, view=IdleRPGStarterView())
    except Exception:
        return None, "failed"

    try:
        await msg.pin(reason="IdleRPG starter panel")
    except Exception:
        pass
    return msg, "posted"


async def _ensure_idlerpg_status_panel(panel_ch: discord.TextChannel) -> tuple[discord.Message | None, str]:
    # Reuse existing pinned status panel to avoid duplicates.
    try:
        pinned = await panel_ch.pins()
        for p in pinned:
            if p.author and p.author.id == client.user.id and p.embeds:
                if (p.embeds[0].title or "").strip().lower() == "idlerpg status panel":
                    return p, "already exists"
    except Exception:
        pass

    embed = discord.Embed(
        title="IdleRPG Status Panel",
        description="Tap the button to pull your live character status without typing /status.",
        color=0x3498DB,
    )
    embed.add_field(name="What It Does", value="Shows your current level, build, inventory, gear, and mission state.", inline=False)
    embed.add_field(name="Tip", value="If your mission timer is done, this status call resolves rewards too.", inline=False)
    embed.set_footer(text="IdleRPG status panel • one-click check")

    try:
        msg = await panel_ch.send(embed=embed, view=IdleRPGStatusPanelView())
    except Exception:
        return None, "failed"

    try:
        await msg.pin(reason="IdleRPG status panel")
    except Exception:
        pass
    return msg, "posted"


@client.tree.command(name="setupidlerpgstatuspanel", description="Post the persistent IdleRPG status panel (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setupidlerpgstatuspanel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    panel_ch = guild.get_channel(IDLERPG_CHAT_CHANNEL_ID)
    if not isinstance(panel_ch, discord.TextChannel):
        await interaction.followup.send(f"IdleRPG channel ID `{IDLERPG_CHAT_CHANNEL_ID}` was not found.", ephemeral=True)
        return

    panel_msg, panel_status = await _ensure_idlerpg_status_panel(panel_ch)
    if panel_msg is None:
        await interaction.followup.send(f"Could not create status panel in {panel_ch.mention}.", ephemeral=True)
        return

    await interaction.followup.send(f"IdleRPG status panel {panel_status} in {panel_ch.mention}.", ephemeral=True)
    await _log_admin_cmd(interaction, "setupidlerpgstatuspanel", f"Status panel {panel_status} in {panel_ch.mention}")


@client.tree.command(name="idlerpghelp", description="Open the IdleRPG quick guide", guild=GUILD_ID)
async def idlerpg_help(interaction: discord.Interaction):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    embed = discord.Embed(
        title="IdleRPG Command Hub",
        description="Build your hero, deploy timed quests, then check-in via status to resolve rewards.",
        color=0x5865F2,
    )
    embed.add_field(name="Build", value="`/create` `/race` `/class` `/alignment` `/follow`", inline=False)
    embed.add_field(name="Mission", value="`/adventure` starts timed run\n`/status` resolves completed run", inline=False)
    embed.add_field(name="Inventory", value="`/inventory` `/equip` `/sell` `/sacrifice` `/ex` `/open`", inline=False)
    embed.add_field(name="Store", value="`/idlerpgshop` for class-themed gear crates and quick chest access.", inline=False)
    embed.add_field(name="Click Flow", value="Use `/idlerpgpanel` and `/idlerpgcodex` for one-click controls and full game info.", inline=False)
    embed.set_footer(text="Use the buttons below for quick guidance.")
    await interaction.response.send_message(embed=embed, view=IdleRPGHelpView(), ephemeral=True)


@client.tree.command(name="idlerpgcodex", description="Open the clickable IdleRPG codex", guild=GUILD_ID)
async def idlerpg_codex(interaction: discord.Interaction):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=_idlerpg_codex_embed("overview"),
        view=IdleRPGCodexView(interaction.user.id),
        ephemeral=True,
    )


@client.tree.command(name="idlerpgpanel", description="Open the clickable IdleRPG control panel", guild=GUILD_ID)
async def idlerpg_panel(interaction: discord.Interaction):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return
    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("Create your character first with /create.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Adventurer's Guild Board",
        description="Use these controls to run your hero journey with fewer slash commands.",
        color=0x1ABC9C,
    )
    embed.add_field(name="Quest", value="Start timed runs, then check status to resolve rewards.", inline=False)
    embed.add_field(name="Gear Loop", value="After status resolves a drop, use reward buttons to equip/sell/keep.", inline=False)
    embed.add_field(name="Shop", value="Open the class crate store from the panel buttons or /idlerpgshop.", inline=False)
    embed.set_footer(text="Panel is player-bound; buttons only work for you.")
    await interaction.response.send_message(embed=embed, view=IdleRPGAdventureView(interaction.user.id), ephemeral=True)


@client.tree.command(name="idlerpgshop", description="Open the IdleRPG class crate store", guild=GUILD_ID)
@app_commands.describe(public="Post shop panel in chat (default is private)")
async def idlerpg_shop(interaction: discord.Interaction, public: bool = False):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("Create your character first with /create.", ephemeral=True)
        return

    shop_ephemeral = not public

    coins = int(profile.get("money", 0))
    class_name = profile.get("class") or "Unchosen"
    god_name = profile.get("god") or "Unchosen"
    embed = discord.Embed(
        title="Arcane Bazaar - Gear Crates",
        description="Buy class-themed crates with coins and open chests directly from this panel.",
        color=0x2980B9,
    )
    embed.add_field(name="Your Coins", value=f"**{coins}**", inline=True)
    embed.add_field(name="Crate Cost", value=f"**{IDLERPG_CLASS_CRATE_COST}** each", inline=True)
    embed.add_field(name="Build", value=f"Class: **{class_name}**\nGod: **{god_name}**", inline=True)
    embed.add_field(
        name="Crate Rules",
        value=(
            "Each crate gives class-themed gear.\n"
            "10% chance to also drop a bonus chest."
        ),
        inline=False,
    )
    embed.set_footer(text="Use the buttons below to buy crates or open chests.")
    await interaction.response.send_message(embed=embed, view=IdleRPGShopView(interaction.user.id), ephemeral=shop_ephemeral)


@client.tree.command(name="create", description="Create your IdleRPG character", guild=GUILD_ID)
@app_commands.describe(name="Your character name")
async def idlerpg_create(interaction: discord.Interaction, name: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    existing = _idlerpg_profile(interaction.user)
    if existing:
        await interaction.response.send_message("You already have a character. Use /status to view it.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user, create=True)
    profile["name"] = name[:32]
    _save_idlerpg_data()

    embed = discord.Embed(
        title="IdleRPG Character Created",
        description=(
            f"Welcome, **{profile['name']}**. Your shell is online.\n"
            "Before adventuring, complete your build path:"
        ),
        color=0x2ECC71,
    )
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Level", value="1", inline=True)
    embed.add_field(name="Coins", value="100", inline=True)
    embed.add_field(name="Build Step 1", value="Choose race with **/race**", inline=False)
    embed.add_field(name="Build Step 2", value="Choose class with **/class**", inline=False)
    embed.add_field(name="Build Step 3", value="Choose alignment with **/alignment**", inline=False)
    embed.add_field(name="Build Step 4", value="Choose god with **/follow**", inline=False)
    embed.set_footer(text="You can only run /adventure after all 4 build steps are complete.")
    await interaction.response.send_message(embed=embed, view=IdleRPGBuildView(interaction.user.id), ephemeral=True)


@client.tree.command(name="status", description="View your IdleRPG character status", guild=GUILD_ID)
@app_commands.describe(public="Post status in chat (default is private)")
async def idlerpg_status(interaction: discord.Interaction, public: bool = False):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    status_ephemeral = not public

    inventory = profile.get("inventory", [])
    magic_chests = sum(1 for i in inventory if i.get("type") == "chest" and str(i.get("tier", "magic")).lower() != "mythic")
    mythic_chests = sum(1 for i in inventory if i.get("type") == "chest" and str(i.get("tier", "magic")).lower() == "mythic")
    rarity_counts = {"Common": 0, "Uncommon": 0, "Rare": 0, "Epic": 0, "Legendary": 0, "Mythic": 0}
    for item in inventory:
        rarity_label, _, _ = _idlerpg_item_rarity(item)
        if rarity_label in rarity_counts:
            rarity_counts[rarity_label] += 1

    equipped = profile.get("equipped")
    equip_text = f"{equipped.get('emoji', '')} {equipped.get('name', 'None')}" if isinstance(equipped, dict) else "None"

    embed = discord.Embed(title=f"{profile['name']} • IdleRPG Status", color=0x3498DB)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Level", value=str(profile.get("level", 1)), inline=True)
    embed.add_field(name="XP", value=str(profile.get("xp", 0)), inline=True)
    embed.add_field(name="Coins", value=f"{profile.get('money', 0)}", inline=True)
    embed.add_field(name="God", value=profile.get("god") or "None", inline=True)
    embed.add_field(name="Favor", value=str(profile.get("favor", 0)), inline=True)
    embed.add_field(name="Equipped", value=equip_text, inline=False)
    embed.add_field(name="Race", value=profile.get("race") or "None", inline=True)
    embed.add_field(name="Class", value=profile.get("class") or "None", inline=True)
    embed.add_field(name="Alignment", value=profile.get("alignment") or "None", inline=True)
    embed.add_field(name="Inventory", value=str(len(inventory)), inline=True)
    embed.add_field(name="Chests", value=f"🧰 {magic_chests} | 🪬 {mythic_chests}", inline=False)
    embed.add_field(
        name="Game Pools",
        value=(
            f"Loot: **{len(IDLERPG_LOOT_POOL)}** | General Gear: **{len(IDLERPG_GEAR_POOL)}** | "
            f"Class Gear: **{sum(len(v) for v in IDLERPG_CLASS_GEAR_POOLS.values())}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Rarity Summary",
        value=(
            f"⚪ {rarity_counts['Common']} Common | 🟢 {rarity_counts['Uncommon']} Uncommon | 🔵 {rarity_counts['Rare']} Rare\n"
            f"🟣 {rarity_counts['Epic']} Epic | 🟡 {rarity_counts['Legendary']} Legendary | 🪬 {rarity_counts['Mythic']} Mythic"
        ),
        inline=False,
    )

    mission_result = None
    reward_view = None
    adv = profile.get("active_adventure")
    if isinstance(adv, dict):
        now_ts = int(time.time())
        end_ts = int(adv.get("end_ts", now_ts))
        if now_ts >= end_ts:
            mission_result = _idlerpg_resolve_adventure(profile)
            _save_idlerpg_data()
        else:
            start_ts = int(adv.get("start_ts", now_ts))
            total = max(1, end_ts - start_ts)
            done = max(0, now_ts - start_ts)
            ratio = done / total
            bar = _idlerpg_progress_bar(ratio)
            embed.add_field(
                name="Active Mission",
                value=(
                    f"{adv.get('encounter_emoji', '⚡')} **{adv.get('encounter', 'Mission')}**\n"
                    f"Progress `{bar}` {int(ratio * 100)}%\n"
                    f"Ends <t:{end_ts}:R>"
                ),
                inline=False,
            )

    if mission_result:
        lvl_text = ""
        if mission_result.get("new_level", 1) > mission_result.get("old_level", 1):
            lvl_text = f"\nLevel up: **{mission_result['old_level']} -> {mission_result['new_level']}**"
        item = mission_result.get("item", {})
        embed.add_field(
            name=f"Mission Complete - {mission_result.get('encounter_emoji', '⚡')} {mission_result.get('encounter', 'Run')}",
            value=(
                f"{mission_result.get('flavor', 'Mission complete.')}\n"
                f"XP +{mission_result.get('xp', 0)} | Coins +{mission_result.get('coins', 0)} | Favor +{mission_result.get('favor', 0)}\n"
                f"Loot: {item.get('emoji', '')} **{item.get('name', 'Unknown')}** (`{item.get('id', '-')}`)"
                f"{lvl_text}"
            ),
            inline=False,
        )
        item = mission_result.get("item", {})
        if isinstance(item, dict) and item.get("id") is not None:
            reward_view = IdleRPGRewardChoiceView(interaction.user.id, int(item.get("id")))

    missing = _idlerpg_build_missing(profile)
    total_build_steps = 4
    if missing:
        embed.add_field(name="Build Progress", value=f"Incomplete ({total_build_steps - len(missing)}/{total_build_steps})\nMissing: " + ", ".join(missing), inline=False)
        embed.set_footer(text="Complete your build path to unlock /adventure.")
    else:
        embed.add_field(name="Build Progress", value="Complete (4/4) - Adventure unlocked", inline=False)
        embed.set_footer(text="Build complete. You are ready for /adventure.")

    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(
        embed=embed,
        view=IdleRPGAdventureView(interaction.user.id),
        ephemeral=status_ephemeral,
    )

    if reward_view:
        await interaction.followup.send(
            "Reward action panel: choose what to do with your latest drop.",
            view=reward_view,
            ephemeral=True,
        )


@client.tree.command(name="adventure", description="Go on an IdleRPG adventure", guild=GUILD_ID)
async def idlerpg_adventure(interaction: discord.Interaction):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    missing = _idlerpg_build_missing(profile)
    if missing:
        embed = discord.Embed(
            title="Build Required Before Adventure",
            description="Finish your character build path first.",
            color=0xE67E22,
        )
        embed.add_field(name="Missing", value="\n".join([f"- {step}" for step in missing]), inline=False)
        embed.set_footer(text="After completing these, run /adventure again.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    active = profile.get("active_adventure")
    if isinstance(active, dict):
        now_ts = int(time.time())
        end_ts = int(active.get("end_ts", now_ts))
        if now_ts < end_ts:
            embed = discord.Embed(
                title="Adventure Already In Progress",
                description=(
                    f"{active.get('encounter_emoji', '⚡')} **{active.get('encounter', 'Mission')}** is underway.\n"
                    f"Ends <t:{end_ts}:R>"
                ),
                color=0xF1C40F,
            )
            embed.set_footer(text="Use /status to check your character and mission progress.")
            await interaction.response.send_message(embed=embed, view=IdleRPGAdventureView(interaction.user.id), ephemeral=True)
            return
        embed = discord.Embed(
            title="Mission Ready To Resolve",
            description="Your previous mission is complete. Use /status to resolve rewards.",
            color=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed, view=IdleRPGAdventureView(interaction.user.id), ephemeral=True)
        return
    profile["active_adventure"] = _idlerpg_prepare_adventure(profile)
    adv = profile["active_adventure"]
    end_ts = int(adv.get("end_ts", int(time.time())))

    embed = discord.Embed(
        title=f"Adventure Deployed - {adv.get('encounter_emoji', '⚡')} {adv.get('encounter', 'Mission')}",
        description=adv.get("flavor", "Mission started."),
        color=0x00B894 if adv.get("encounter") != "Boss Event" else 0x8E44AD,
    )
    embed.set_author(name=profile.get("name", str(interaction.user)), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Launch", value=f"<t:{int(adv.get('start_ts', int(time.time())))}:t>", inline=True)
    embed.add_field(name="ETA", value=f"<t:{end_ts}:R>", inline=True)
    embed.add_field(name="Resolve", value="Use **/status** to check and claim mission rewards.", inline=False)
    embed.add_field(name="Build", value=f"{profile.get('race')} • {profile.get('class')} • {profile.get('alignment')} • {profile.get('god')}", inline=False)
    embed.set_footer(text="Timed mission active. Progress updates are visible in /status.")
    embed.timestamp = discord.utils.utcnow()

    _save_idlerpg_data()
    await interaction.response.send_message(embed=embed, view=IdleRPGAdventureView(interaction.user.id), ephemeral=True)


@client.tree.command(name="inventory", description="View your IdleRPG inventory", guild=GUILD_ID)
@app_commands.describe(public="Post inventory in chat (default is private)")
async def idlerpg_inventory(interaction: discord.Interaction, public: bool = False):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    inv_ephemeral = not public

    inv = profile.get("inventory", [])
    if not inv:
        await interaction.response.send_message("Your inventory is empty. Run /adventure.", ephemeral=True)
        return

    lines = []
    top_color = 0x95A5A6
    for item in inv[:20]:
        rarity_label, rarity_color, rarity_marker = _idlerpg_item_rarity(item)
        if top_color == 0x95A5A6:
            top_color = rarity_color
        lines.append(
            f"{rarity_marker} `{item.get('id')}` {item.get('emoji', '')} **{item.get('name', 'Unknown')}** [{rarity_label}] ({item.get('type', 'item')})"
        )
    embed = discord.Embed(title=f"{profile.get('name', 'Player')} • Inventory", description="\n".join(lines), color=top_color)
    if len(inv) > 20:
        embed.set_footer(text=f"Showing 20/{len(inv)} items")
    await interaction.response.send_message(embed=embed, ephemeral=inv_ephemeral)


@client.tree.command(name="equip", description="Equip an item by ID", guild=GUILD_ID)
@app_commands.describe(item_id="Item ID from /inventory")
async def idlerpg_equip(interaction: discord.Interaction, item_id: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    item = next((i for i in profile.get("inventory", []) if str(i.get("id")) == str(item_id)), None)
    if not item:
        await interaction.response.send_message("Item not found in your inventory.", ephemeral=True)
        return
    if item.get("type") != "gear":
        await interaction.response.send_message("Only gear items can be equipped.", ephemeral=True)
        return

    profile["equipped"] = item
    _save_idlerpg_data()
    await interaction.response.send_message(f"Equipped {item.get('emoji', '')} **{item.get('name', 'item')}**.", ephemeral=True)


@client.tree.command(name="sell", description="Sell an item by ID", guild=GUILD_ID)
@app_commands.describe(item_id="Item ID from /inventory")
async def idlerpg_sell(interaction: discord.Interaction, item_id: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    inv = profile.get("inventory", [])
    item = next((i for i in inv if str(i.get("id")) == str(item_id)), None)
    if not item:
        await interaction.response.send_message("Item not found in your inventory.", ephemeral=True)
        return
    if item.get("type") in {"loot", "chest"}:
        await interaction.response.send_message("Loot/chests cannot be sold. Use /sacrifice or /open.", ephemeral=True)
        return

    inv.remove(item)
    coins = int(item.get("value", 50))
    profile["money"] = int(profile.get("money", 0)) + coins
    _save_idlerpg_data()
    await interaction.response.send_message(f"Sold **{item.get('name', 'item')}** for **{coins}** coins.", ephemeral=True)


@client.tree.command(name="follow", description="Choose a god", guild=GUILD_ID)
@app_commands.describe(god="Warrior=Kord, Mage=Mystra, Rogue=Mask, Cleric=Lathander, Ranger=Mielikki, Paladin=Bahamut")
async def idlerpg_follow(interaction: discord.Interaction, god: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    class_name = profile.get("class")
    if not class_name:
        await interaction.response.send_message("Choose your class first with /class, then pick your aligned god with /follow.", ephemeral=True)
        return

    pick = next((g for g in IDLERPG_GODS if g["name"].lower() == god.lower()), None)
    if not pick:
        options = "\n".join([f"{g['emoji']} **{g['class']} -> {g['name']}** - {g['desc']}" for g in IDLERPG_GODS])
        await interaction.response.send_message(f"Invalid god. Choose one:\n{options}", ephemeral=True)
        return

    expected = _idlerpg_god_for_class(class_name)
    if not expected or pick["name"] != expected["name"]:
        await interaction.response.send_message(
            f"Class **{class_name}** aligns with **{expected['name'] if expected else 'no god'}**. Use that god for /follow.",
            ephemeral=True,
        )
        return

    profile["god"] = pick["name"]
    _save_idlerpg_data()
    await interaction.response.send_message(f"You now follow {pick['emoji']} **{pick['name']}**.")


@client.tree.command(name="sacrifice", description="Sacrifice a loot item for favor", guild=GUILD_ID)
@app_commands.describe(item_id="Loot item ID")
async def idlerpg_sacrifice(interaction: discord.Interaction, item_id: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    inv = profile.get("inventory", [])
    item = next((i for i in inv if str(i.get("id")) == str(item_id)), None)
    if not item or item.get("type") != "loot":
        await interaction.response.send_message("Loot item not found.", ephemeral=True)
        return

    inv.remove(item)
    gain = int(item.get("favor", 10))
    profile["favor"] = int(profile.get("favor", 0)) + gain
    _save_idlerpg_data()
    await interaction.response.send_message(f"Sacrificed **{item.get('name', 'loot')}**. Favor +**{gain}**.")


@client.tree.command(name="ex", description="Sacrifice all loot for favor", guild=GUILD_ID)
async def idlerpg_ex(interaction: discord.Interaction):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    inv = profile.get("inventory", [])
    loot = [i for i in inv if i.get("type") == "loot"]
    if not loot:
        await interaction.response.send_message("No loot available to sacrifice.", ephemeral=True)
        return

    favor = sum(int(i.get("favor", 10)) for i in loot)
    profile["inventory"] = [i for i in inv if i.get("type") != "loot"]
    profile["favor"] = int(profile.get("favor", 0)) + favor
    _save_idlerpg_data()
    await interaction.response.send_message(f"Mass sacrifice complete. Favor +**{favor}**.")


@client.tree.command(name="race", description="Choose your race", guild=GUILD_ID)
@app_commands.describe(race="Pick any available species shown in /idlerpgcodex")
async def idlerpg_race(interaction: discord.Interaction, race: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    pick = next((r for r in IDLERPG_RACES if r["name"].lower() == race.lower()), None)
    if not pick:
        options = "\n".join([f"{r['emoji']} **{r['name']}** - {r['desc']}" for r in IDLERPG_RACES])
        await interaction.response.send_message(f"Invalid race. Choose one:\n{options}", ephemeral=True)
        return

    profile["race"] = pick["name"]
    _save_idlerpg_data()
    await interaction.response.send_message(f"Race selected: {pick['emoji']} **{pick['name']}**.")


@client.tree.command(name="class", description="Choose your class", guild=GUILD_ID)
@app_commands.describe(class_name="Warrior, Mage, Rogue, Cleric, Ranger, or Paladin")
async def idlerpg_class(interaction: discord.Interaction, class_name: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    pick = next((c for c in IDLERPG_CLASSES if c["name"].lower() == class_name.lower()), None)
    if not pick:
        options = "\n".join([f"{c['emoji']} **{c['name']}** - {c['desc']}" for c in IDLERPG_CLASSES])
        await interaction.response.send_message(f"Invalid class. Choose one:\n{options}", ephemeral=True)
        return

    profile["class"] = pick["name"]
    _save_idlerpg_data()
    await interaction.response.send_message(f"Class selected: {pick['emoji']} **{pick['name']}**.")


@client.tree.command(name="alignment", description="Choose your moral alignment", guild=GUILD_ID)
@app_commands.describe(alignment="Examples: Lawful Good, True Neutral, Chaotic Evil")
async def idlerpg_alignment(interaction: discord.Interaction, alignment: str):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    pick = next((a for a in IDLERPG_ALIGNMENTS if a["name"].lower() == alignment.lower()), None)
    if not pick:
        options = "\n".join([f"{a['emoji']} **{a['name']}** - {a['desc']}" for a in IDLERPG_ALIGNMENTS])
        await interaction.response.send_message(f"Invalid alignment. Choose one:\n{options}", ephemeral=True)
        return

    profile["alignment"] = pick["name"]
    _save_idlerpg_data()
    await interaction.response.send_message(f"Alignment selected: {pick['emoji']} **{pick['name']}**.")


@client.tree.command(name="open", description="Open one magic chest", guild=GUILD_ID)
async def idlerpg_open(interaction: discord.Interaction):
    allowed, target_ch = _idlerpg_allowed_channel(interaction)
    if not allowed:
        await interaction.response.send_message(f"Use IdleRPG commands in {target_ch.mention}.", ephemeral=True)
        return

    profile = _idlerpg_profile(interaction.user)
    if not profile:
        await interaction.response.send_message("No character found. Run /create first.", ephemeral=True)
        return

    inv = profile.get("inventory", [])
    chest = next((i for i in inv if i.get("type") == "chest"), None)
    if not chest:
        await interaction.response.send_message("No magic chest found. Run /adventure.", ephemeral=True)
        return

    inv.remove(chest)
    chest_tier = str(chest.get("tier", "magic")).lower()
    chest_name = chest.get("name", "Magic Chest")
    chest_emoji = chest.get("emoji", "🧰")

    if chest_tier == "mythic":
        coins = random.randint(420, 900)
        if random.random() < 0.35:
            bonus_item = _idlerpg_make_item(kind="super_chest", profile=profile)
        else:
            bonus_item = _idlerpg_make_item(kind="gear", profile=profile)
    else:
        coins = random.randint(180, 420)
        bonus_item = _idlerpg_make_item(kind="gear", profile=profile)

    inv.append(bonus_item)
    profile["money"] = int(profile.get("money", 0)) + coins
    _save_idlerpg_data()

    bonus_rarity, bonus_color, bonus_marker = _idlerpg_item_rarity(bonus_item)
    accent = 0xF1C40F if chest_tier == "mythic" else bonus_color
    embed = discord.Embed(
        title=f"{chest_emoji} {chest_name} Opened",
        description="The chest unlocks and pulses with arcane energy.",
        color=accent,
    )
    embed.add_field(name="Coins", value=f"**+{coins}**", inline=True)
    embed.add_field(name="Treasure", value=f"{bonus_marker} {bonus_item.get('emoji', '🎁')} **{bonus_item.get('name', 'Unknown Relic')}** [{bonus_rarity}]", inline=True)
    embed.add_field(
        name="Rarity",
        value="Mythic chest opened. Increased reward tier applied." if chest_tier == "mythic" else "Magic chest opened. Standard reward tier applied.",
        inline=False,
    )
    embed.set_footer(text="Chest drops come from adventures. Mythic chests are super rare.")
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="fixchannels", description="Fix specific channel names and emojis (Admin only)")
@app_commands.default_permissions(administrator=True)
async def fixchannels(interaction: discord.Interaction):
    """Fix ticket log emoji and ensure streamer channel exists with proper naming."""
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    await _apply_requested_channel_repairs(guild)

    ticket_ch = guild.get_channel(TICKET_LOG_CHANNEL_ID)
    streamer_ch = _resolve_streamer_channel(guild)
    streamer_parent = _resolve_streamer_parent(guild)

    lines = ["**Channel Fixes:**"]
    if isinstance(ticket_ch, discord.TextChannel):
        lines.append(f"✅ Ticket log: {ticket_ch.mention} (`{ticket_ch.name}`)")
    else:
        lines.append(f"âš ï¸ Ticket log ID `{TICKET_LOG_CHANNEL_ID}` not found in this guild")

    if isinstance(streamer_ch, discord.TextChannel):
        parent_text = f" in `{streamer_parent.name}`" if streamer_parent else ""
        lines.append(f"✅ Streamer channel: {streamer_ch.mention} (`{streamer_ch.name}`){parent_text}")
    else:
        lines.append("âš ï¸ Streamer channel could not be resolved after repair")

    await interaction.followup.send("\n".join(lines), ephemeral=True)
    await _log_admin_cmd(interaction, "fixchannels", " | ".join(lines[1:]))


@client.tree.command(name="setupverify", description="Create (or reset) the #✅-verify channel with the verification embed (Admin only)")
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
            f"âš”ï¸ **#{POKEMON_CHANNEL_NAME}** — Pokemon battles\n"
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


@client.tree.command(name="createchannel", description="Create a new text channel (Admin only)")
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
            await interaction.followup.send(f"âŒ No category named **{category}** found.", ephemeral=True)
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
        await interaction.followup.send(f"âŒ No category named **{category}** found. Check the exact name and try again.", ephemeral=True)
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

