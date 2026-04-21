"""
Bot Web Dashboard — Flask app that runs in a background thread alongside the Discord bot.
Access at http://localhost:5000 while the bot is running.

Environment variables:
  DASHBOARD_SECRET_KEY — Flask session secret    (random per-run if not set)
  DASHBOARD_HOST       — bind address            (default: 127.0.0.1)
  DASHBOARD_PORT       — port                    (default: 5000)

Admin accounts are defined in the ADMIN_ACCOUNTS dict below.
Add entries as  "username": "password"  to grant access to more people.
"""

import os
import threading
import logging
import datetime
import asyncio

import discord
from flask import (
    Flask, render_template, request,
    redirect, url_for, session, jsonify,
)
from werkzeug.middleware.proxy_fix import ProxyFix

logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)
# Trust Railway/ngrok reverse-proxy headers so HTTPS sessions work correctly
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or os.urandom(24)
# Secure cookie settings for HTTPS custom domain
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or 5000)

# ── Admin accounts ────────────────────────────────────────────────────────────
# Add or remove entries here to control who can log in.
# Format:  "username": "password"
ADMIN_ACCOUNTS: dict[str, str] = {
    "admin": "admin123",
    "mod1": "Mod4",
    "mod2": "Mod7",
}

# Shared state injected by Main1.py
_state: dict = {}
_started = False


def init(
    xp_data,
    voice_minutes,
    invite_counts,
    open_tickets,
    giveaways,
    streamers,
    banned_words,
    banned_word_warnings,
    whitelist,
    music_states,
    reaction_roles,
    bot_client,
    guild_id: int,
    bot_loop,
    search_youtube_fn=None,
    play_next_fn=None,
    song_entry_cls=None,
    get_music_state_fn=None,
) -> None:
    _state.update(dict(
        xp_data=xp_data,
        voice_minutes=voice_minutes,
        invite_counts=invite_counts,
        open_tickets=open_tickets,
        giveaways=giveaways,
        streamers=streamers,
        banned_words=banned_words,
        banned_word_warnings=banned_word_warnings,
        whitelist=whitelist,
        music_states=music_states,
        reaction_roles=reaction_roles,
        client=bot_client,
        guild_id=guild_id,
        bot_loop=bot_loop,
        search_youtube_fn=search_youtube_fn,
        play_next_fn=play_next_fn,
        song_entry_cls=song_entry_cls,
        get_music_state_fn=get_music_state_fn,
    ))


def _run_flask() -> None:
    try:
        app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, use_reloader=False)
    except OSError as e:
        print(f"[Dashboard] ERROR: Could not bind to port {DASHBOARD_PORT} — another bot instance may be running. ({e})")


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_run_flask, daemon=True, name="dashboard-web")
    t.start()
    users = ", ".join(ADMIN_ACCOUNTS.keys())
    print(f"[Dashboard] http://{DASHBOARD_HOST}:{DASHBOARD_PORT}  (accounts: {users})")


# XP helpers (mirror Main1.py)
def _xp_required(level: int) -> int:
    return 100 * (level ** 2) + 50 * level + 100

def _xp_to_level(xp: int) -> int:
    level = 0
    while xp >= _xp_required(level + 1):
        xp -= _xp_required(level + 1)
        level += 1
    return level


# Auth
def _logged_in() -> bool:
    return session.get("logged_in") is True

def _current_user() -> str:
    return session.get("username", "")

def _unauth():
    return jsonify({"error": "unauthorized"}), 401


# Discord helpers
def _guild():
    client = _state.get("client")
    gid    = _state.get("guild_id")
    return client.get_guild(gid) if client else None

def _member_name(uid: int) -> str:
    g = _guild()
    if g:
        m = g.get_member(uid)
        if m:
            return m.display_name
    return f"User {uid}"

def _run(coro, timeout=15):
    """Run a coroutine on the bot event loop and return the result (blocks caller thread)."""
    bot_loop = _state.get("bot_loop")
    if not bot_loop:
        raise RuntimeError("Bot loop not available")
    future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
    return future.result(timeout=timeout)


# PAGE ROUTES
@app.route("/")
def index():
    return redirect(url_for("dashboard") if _logged_in() else url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username in ADMIN_ACCOUNTS and ADMIN_ACCOUNTS[username] == password:
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("dashboard"))
        error = "Incorrect username or password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if not _logged_in():
        return redirect(url_for("login"))
    return render_template("dashboard.html", username=_current_user())


# API: STATS / OVERVIEW
@app.route("/api/stats")
def api_stats():
    if not _logged_in(): return _unauth()
    client       = _state.get("client")
    guild_id     = _state.get("guild_id")
    guild        = _guild()
    xp_data      = _state.get("xp_data", {})
    voice_mins   = _state.get("voice_minutes", {})
    inv_counts   = _state.get("invite_counts", {})
    open_tickets = _state.get("open_tickets", {})
    giveaways    = _state.get("giveaways", {})
    streamers    = _state.get("streamers", [])
    music_states = _state.get("music_states", {})
    guild_xp      = xp_data.get(guild_id, {})
    guild_voice   = voice_mins.get(guild_id, {})
    guild_invites = inv_counts.get(guild_id, {})
    xp_top     = sorted(guild_xp.items(),      key=lambda x: x[1], reverse=True)[:15]
    voice_top  = sorted(guild_voice.items(),   key=lambda x: x[1], reverse=True)[:10]
    invite_top = sorted(guild_invites.items(), key=lambda x: x[1], reverse=True)[:10]
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    giveaway_list = [
        {"message_id": str(mid), "prize": g["prize"], "winners": g["winners"],
         "ends_in": max(0, int(g["end_time"] - now_ts)), "ended": g["ended"]}
        for mid, g in giveaways.items()
    ]
    music_info = None
    if guild_id and guild_id in music_states:
        ms = music_states[guild_id]
        if ms.current:
            music_info = {
                "title": ms.current.title, "url": ms.current.webpage_url,
                "duration": ms.current.format_duration(), "requester": str(ms.current.requester),
                "queue_length": len(ms.queue), "volume": int(ms.volume * 100),
            }
    return jsonify({
        "bot_online":       bool(client and client.user),
        "bot_name":         str(client.user) if (client and client.user) else "Unknown",
        "guild_name":       guild.name if guild else "Unknown",
        "member_count":     guild.member_count if guild else 0,
        "open_tickets":     len(open_tickets),
        "active_giveaways": sum(1 for g in giveaways.values() if not g["ended"]),
        "streamers_live":   sum(1 for s in streamers if s.get("last_live")),
        "streamers_total":  len(streamers),
        "xp_leaderboard":  [{"name": _member_name(uid), "xp": xp, "level": _xp_to_level(xp)} for uid, xp in xp_top],
        "voice_leaderboard":[{"name": _member_name(uid), "minutes": mins} for uid, mins in voice_top],
        "invite_leaderboard":[{"name": _member_name(uid), "invites": count} for uid, count in invite_top],
        "giveaways":        giveaway_list,
        "streamers":        list(streamers),
        "music":            music_info,
    })


# API: MEMBERS / CHANNELS / ROLES
@app.route("/api/members")
def api_members():
    if not _logged_in(): return _unauth()
    guild = _guild()
    if not guild:
        return jsonify({"members": []})
    members = [
        {"id": str(m.id), "name": str(m), "display": m.display_name}
        for m in sorted(guild.members, key=lambda m: m.display_name.lower())
        if not m.bot
    ]
    return jsonify({"members": members})

@app.route("/api/channels")
def api_channels():
    if not _logged_in(): return _unauth()
    guild = _guild()
    if not guild:
        return jsonify({"channels": []})
    channels = [
        {"id": str(c.id), "name": c.name}
        for c in sorted(guild.text_channels, key=lambda c: c.name)
    ]
    return jsonify({"channels": channels})

@app.route("/api/roles")
def api_roles():
    if not _logged_in(): return _unauth()
    guild = _guild()
    if not guild:
        return jsonify({"roles": []})
    roles = [
        {"id": str(r.id), "name": r.name}
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        if not r.is_default()
    ]
    return jsonify({"roles": roles})


# API: MODERATION ACTIONS
@app.route("/api/mod/ban", methods=["POST"])
def api_mod_ban():
    if not _logged_in(): return _unauth()
    data   = request.json or {}
    uid    = data.get("user_id", "").strip()
    reason = data.get("reason", "Banned via dashboard").strip() or "Banned via dashboard"
    try:
        uid_int = int(uid)
    except ValueError:
        return jsonify({"error": "Invalid user ID"}), 400
    async def _do():
        guild = _guild()
        member = guild.get_member(uid_int)
        if not member:
            raise ValueError(f"Member {uid_int} not found in server")
        if uid_int in _state.get("whitelist", set()):
            raise ValueError("That member is whitelisted and cannot be banned")
        await member.ban(reason=reason)
        return str(member)
    try:
        name = _run(_do())
        return jsonify({"ok": True, "message": f"Banned {name}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/mod/kick", methods=["POST"])
def api_mod_kick():
    if not _logged_in(): return _unauth()
    data   = request.json or {}
    uid    = data.get("user_id", "").strip()
    reason = data.get("reason", "Kicked via dashboard").strip() or "Kicked via dashboard"
    try:
        uid_int = int(uid)
    except ValueError:
        return jsonify({"error": "Invalid user ID"}), 400
    async def _do():
        guild = _guild()
        member = guild.get_member(uid_int)
        if not member:
            raise ValueError("Member not found in server")
        if uid_int in _state.get("whitelist", set()):
            raise ValueError("That member is whitelisted and cannot be kicked")
        await member.kick(reason=reason)
        return str(member)
    try:
        name = _run(_do())
        return jsonify({"ok": True, "message": f"Kicked {name}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/mod/timeout", methods=["POST"])
def api_mod_timeout():
    if not _logged_in(): return _unauth()
    data    = request.json or {}
    uid     = data.get("user_id", "").strip()
    minutes = int(data.get("minutes", 10))
    reason  = data.get("reason", "Timed out via dashboard").strip() or "Timed out via dashboard"
    try:
        uid_int = int(uid)
    except ValueError:
        return jsonify({"error": "Invalid user ID"}), 400
    if minutes <= 0:
        return jsonify({"error": "Duration must be > 0"}), 400
    async def _do():
        guild = _guild()
        member = guild.get_member(uid_int)
        if not member:
            raise ValueError("Member not found in server")
        if uid_int in _state.get("whitelist", set()):
            raise ValueError("That member is whitelisted")
        await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
        return str(member)
    try:
        name = _run(_do())
        return jsonify({"ok": True, "message": f"Timed out {name} for {minutes} min"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/mod/unmute", methods=["POST"])
def api_mod_unmute():
    if not _logged_in(): return _unauth()
    uid = (request.json or {}).get("user_id", "").strip()
    try:
        uid_int = int(uid)
    except ValueError:
        return jsonify({"error": "Invalid user ID"}), 400
    async def _do():
        guild = _guild()
        member = guild.get_member(uid_int)
        if not member:
            raise ValueError("Member not found")
        await member.timeout(None)
        return str(member)
    try:
        name = _run(_do())
        return jsonify({"ok": True, "message": f"Unmuted/untimeout {name}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/mod/clear", methods=["POST"])
def api_mod_clear():
    if not _logged_in(): return _unauth()
    data       = request.json or {}
    channel_id = data.get("channel_id", "").strip()
    amount     = int(data.get("amount", 10))
    if amount < 1 or amount > 100:
        return jsonify({"error": "Amount must be 1-100"}), 400
    try:
        cid = int(channel_id)
    except ValueError:
        return jsonify({"error": "Invalid channel ID"}), 400
    async def _do():
        guild = _guild()
        ch = guild.get_channel(cid)
        if not ch:
            raise ValueError("Channel not found")
        deleted = await ch.purge(limit=amount)
        return len(deleted)
    try:
        count = _run(_do())
        return jsonify({"ok": True, "message": f"Deleted {count} messages"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/mod/announce", methods=["POST"])
def api_mod_announce():
    if not _logged_in(): return _unauth()
    data       = request.json or {}
    channel_id = data.get("channel_id", "").strip()
    message    = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400
    if len(message) > 2000:
        return jsonify({"error": "Message too long (max 2000 chars)"}), 400
    try:
        cid = int(channel_id)
    except ValueError:
        return jsonify({"error": "Invalid channel ID"}), 400
    async def _do():
        guild = _guild()
        ch = guild.get_channel(cid)
        if not ch:
            raise ValueError("Channel not found")
        await ch.send(message)
    try:
        _run(_do())
        return jsonify({"ok": True, "message": "Announcement sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/mod/whitelist/add", methods=["POST"])
def api_whitelist_add():
    if not _logged_in(): return _unauth()
    uid = (request.json or {}).get("user_id", "").strip()
    try:
        uid_int = int(uid)
    except ValueError:
        return jsonify({"error": "Invalid user ID"}), 400
    _state["whitelist"].add(uid_int)
    return jsonify({"ok": True, "message": f"Whitelisted {_member_name(uid_int)}"})

@app.route("/api/mod/whitelist/remove", methods=["POST"])
def api_whitelist_remove():
    if not _logged_in(): return _unauth()
    uid = (request.json or {}).get("user_id", "").strip()
    try:
        uid_int = int(uid)
    except ValueError:
        return jsonify({"error": "Invalid user ID"}), 400
    _state["whitelist"].discard(uid_int)
    return jsonify({"ok": True, "message": f"Removed from whitelist"})


# API: BANS
@app.route("/api/bans")
def api_bans():
    if not _logged_in(): return _unauth()
    client   = _state.get("client")
    guild_id = _state.get("guild_id")
    if not client:
        return jsonify({"bans": []})
    async def _fetch():
        guild = client.get_guild(guild_id)
        if not guild:
            return []
        return [{"id": str(e.user.id), "name": str(e.user), "reason": e.reason or ""}
                async for e in guild.bans()]
    try:
        bans = _run(_fetch())
    except Exception:
        bans = []
    return jsonify({"bans": bans})

@app.route("/api/unban", methods=["POST"])
def api_unban():
    if not _logged_in(): return _unauth()
    data    = request.json or {}
    uid_str = data.get("user_id", "")
    reason  = data.get("reason", "Unbanned via dashboard").strip() or "Unbanned via dashboard"
    try:
        uid = int(uid_str)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid user ID"}), 400
    async def _do():
        guild = _guild()
        if not guild:
            raise RuntimeError("Guild not found")
        entry = await guild.fetch_ban(discord.Object(id=uid))
        await guild.unban(entry.user, reason=reason)
        return str(entry.user)
    try:
        username = _run(_do())
        return jsonify({"ok": True, "username": username})
    except discord.NotFound:
        return jsonify({"error": f"No ban found for ID {uid}"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# API: GIVEAWAYS
@app.route("/api/giveaways/start", methods=["POST"])
def api_giveaway_start():
    if not _logged_in(): return _unauth()
    data       = request.json or {}
    prize      = data.get("prize", "").strip()
    channel_id = data.get("channel_id", "").strip()
    minutes    = int(data.get("minutes", 60))
    winners    = int(data.get("winners", 1))
    if not prize:
        return jsonify({"error": "Prize cannot be empty"}), 400
    if minutes < 1:
        return jsonify({"error": "Duration must be at least 1 minute"}), 400
    try:
        cid = int(channel_id)
    except ValueError:
        return jsonify({"error": "Invalid channel ID"}), 400
    giveaways = _state.get("giveaways", {})
    end_time  = datetime.datetime.now(datetime.timezone.utc).timestamp() + minutes * 60
    async def _do():
        guild = _guild()
        ch = guild.get_channel(cid)
        if not ch:
            raise ValueError("Channel not found")
        embed = discord.Embed(
            title="GIVEAWAY!",
            description=(
                f"**Prize:** {prize}\n**Winners:** {winners}\n"
                f"**Ends:** <t:{int(end_time)}:R>\n\nReact with 🎉 to enter!"
            ),
            color=0xF1C40F,
        )
        msg = await ch.send(embed=embed)
        await msg.add_reaction("🎉")
        return msg.id
    try:
        msg_id = _run(_do())
        giveaways[msg_id] = {"channel_id": cid, "end_time": end_time, "prize": prize,
                              "winners": winners, "host_id": 0, "ended": False}
        return jsonify({"ok": True, "message": f"Giveaway started for '{prize}'"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/giveaways/end", methods=["POST"])
def api_giveaway_end():
    if not _logged_in(): return _unauth()
    mid_str = (request.json or {}).get("message_id", "").strip()
    try:
        mid = int(mid_str)
    except ValueError:
        return jsonify({"error": "Invalid message ID"}), 400
    giveaways = _state.get("giveaways", {})
    if mid not in giveaways:
        return jsonify({"error": "Giveaway not found"}), 404
    giveaways[mid]["end_time"] = 0
    return jsonify({"ok": True, "message": "Giveaway will end on next check cycle (~30s)"})


# API: MUSIC
@app.route("/api/music/queue")
def api_music_queue():
    if not _logged_in(): return _unauth()
    guild_id     = _state.get("guild_id")
    music_states = _state.get("music_states", {})
    if not guild_id or guild_id not in music_states:
        return jsonify({"current": None, "queue": [], "volume": 100})
    ms = music_states[guild_id]
    current = None
    if ms.current:
        current = {"title": ms.current.title, "url": ms.current.webpage_url,
                   "duration": ms.current.format_duration(), "requester": str(ms.current.requester)}
    queue = [{"title": e.title, "url": e.webpage_url, "duration": e.format_duration(),
              "requester": str(e.requester)} for e in ms.queue]
    return jsonify({"current": current, "queue": queue, "volume": int(ms.volume * 100)})

@app.route("/api/music/skip", methods=["POST"])
def api_music_skip():
    if not _logged_in(): return _unauth()
    async def _do():
        guild = _guild()
        vc = guild.voice_client
        if not vc or not vc.is_playing():
            raise ValueError("Nothing is playing")
        vc.stop()
    try:
        _run(_do())
        return jsonify({"ok": True, "message": "Skipped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/music/pause", methods=["POST"])
def api_music_pause():
    if not _logged_in(): return _unauth()
    async def _do():
        guild = _guild()
        vc = guild.voice_client
        if not vc or not vc.is_playing():
            raise ValueError("Nothing is playing")
        vc.pause()
    try:
        _run(_do())
        return jsonify({"ok": True, "message": "Paused"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/music/resume", methods=["POST"])
def api_music_resume():
    if not _logged_in(): return _unauth()
    async def _do():
        guild = _guild()
        vc = guild.voice_client
        if not vc or not vc.is_paused():
            raise ValueError("Nothing is paused")
        vc.resume()
    try:
        _run(_do())
        return jsonify({"ok": True, "message": "Resumed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/music/stop", methods=["POST"])
def api_music_stop():
    if not _logged_in(): return _unauth()
    guild_id     = _state.get("guild_id")
    music_states = _state.get("music_states", {})
    async def _do():
        guild = _guild()
        vc = guild.voice_client
        if not vc:
            raise ValueError("Not connected to a voice channel")
        if guild_id in music_states:
            music_states[guild_id].queue.clear()
            music_states[guild_id].current = None
        vc.stop()
    try:
        _run(_do())
        return jsonify({"ok": True, "message": "Stopped and queue cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/music/volume", methods=["POST"])
def api_music_volume():
    if not _logged_in(): return _unauth()
    level        = int((request.json or {}).get("level", 50))
    guild_id     = _state.get("guild_id")
    music_states = _state.get("music_states", {})
    if not 0 <= level <= 100:
        return jsonify({"error": "Volume must be 0-100"}), 400
    async def _do():
        guild = _guild()
        vc = guild.voice_client
        if guild_id in music_states:
            music_states[guild_id].volume = level / 100
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = level / 100
    try:
        _run(_do())
        return jsonify({"ok": True, "message": f"Volume set to {level}%"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# API: BANNED WORDS
@app.route("/api/banned-words")
def api_banned_words():
    if not _logged_in(): return _unauth()
    return jsonify({"words": sorted(_state.get("banned_words", set()))})

@app.route("/api/banned-words/add", methods=["POST"])
def api_banned_words_add():
    if not _logged_in(): return _unauth()
    word = (request.json or {}).get("word", "").strip().lower()
    if not word or len(word) > 50:
        return jsonify({"error": "Invalid word"}), 400
    _state["banned_words"].add(word)
    return jsonify({"words": sorted(_state["banned_words"])})

@app.route("/api/banned-words/remove", methods=["POST"])
def api_banned_words_remove():
    if not _logged_in(): return _unauth()
    word = (request.json or {}).get("word", "").strip().lower()
    _state["banned_words"].discard(word)
    return jsonify({"words": sorted(_state["banned_words"])})


# API: WARNINGS
@app.route("/api/warnings")
def api_warnings():
    if not _logged_in(): return _unauth()
    warnings = _state.get("banned_word_warnings", {})
    return jsonify({"warnings": [
        {"id": str(uid), "name": _member_name(uid), "count": count}
        for uid, count in warnings.items()
    ]})

@app.route("/api/warnings/clear", methods=["POST"])
def api_warnings_clear():
    if not _logged_in(): return _unauth()
    uid_str = (request.json or {}).get("user_id", "")
    try:
        uid = int(uid_str)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid user ID"}), 400
    _state["banned_word_warnings"].pop(uid, None)
    warnings = _state.get("banned_word_warnings", {})
    return jsonify({"warnings": [
        {"id": str(u), "name": _member_name(u), "count": c}
        for u, c in warnings.items()
    ]})


# API: WHITELIST
@app.route("/api/whitelist")
def api_whitelist():
    if not _logged_in(): return _unauth()
    whitelist = _state.get("whitelist", set())
    return jsonify({"members": [
        {"id": str(uid), "name": _member_name(uid)} for uid in whitelist
    ]})


# API: STREAMERS
@app.route("/api/streamers/add", methods=["POST"])
def api_streamers_add():
    if not _logged_in(): return _unauth()
    data     = request.json or {}
    name     = data.get("name", "").strip()
    platform = data.get("platform", "twitch").strip().lower()
    if not name or len(name) > 50:
        return jsonify({"error": "Invalid name"}), 400
    if platform not in ("twitch", "youtube"):
        return jsonify({"error": "Invalid platform"}), 400
    streamers = _state.get("streamers", [])
    for s in streamers:
        if s["name"].lower() == name.lower() and s["platform"] == platform:
            return jsonify({"error": "Already following"}), 400
    streamers.append({"name": name, "platform": platform, "last_live": False})
    return jsonify({"streamers": list(streamers)})

@app.route("/api/streamers/remove", methods=["POST"])
def api_streamers_remove():
    if not _logged_in(): return _unauth()
    name      = (request.json or {}).get("name", "").strip()
    streamers = _state.get("streamers")
    if streamers is not None:
        streamers[:] = [s for s in streamers if s["name"].lower() != name.lower()]
    return jsonify({"streamers": list(streamers or [])})


# API: REACTION ROLES
@app.route("/api/reaction-roles")
def api_reaction_roles():
    if not _logged_in(): return _unauth()
    rr    = _state.get("reaction_roles", {})
    guild = _guild()
    result = []
    for msg_id, mappings in rr.items():
        for emoji, role_id in mappings.items():
            role_name = "Unknown"
            if guild:
                role = guild.get_role(role_id)
                if role:
                    role_name = role.name
            result.append({"message_id": str(msg_id), "emoji": emoji,
                            "role_id": str(role_id), "role_name": role_name})
    return jsonify({"reaction_roles": result})

@app.route("/api/reaction-roles/add", methods=["POST"])
def api_reaction_roles_add():
    if not _logged_in(): return _unauth()
    data       = request.json or {}
    channel_id = data.get("channel_id", "").strip()
    message_id = data.get("message_id", "").strip()
    emoji      = data.get("emoji", "").strip()
    role_id    = data.get("role_id", "").strip()
    if not all([channel_id, message_id, emoji, role_id]):
        return jsonify({"error": "All fields are required"}), 400
    try:
        cid = int(channel_id)
        mid = int(message_id)
        rid = int(role_id)
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400
    rr = _state.get("reaction_roles", {})
    async def _do():
        guild = _guild()
        ch    = guild.get_channel(cid)
        if not ch:
            raise ValueError("Channel not found")
        msg = await ch.fetch_message(mid)
        rr.setdefault(mid, {})[emoji] = rid
        await msg.add_reaction(emoji)
    try:
        _run(_do())
        return jsonify({"ok": True, "message": f"Reaction role added: {emoji}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# API: VOICE CHANNELS
@app.route("/api/voice-channels")
def api_voice_channels():
    if not _logged_in(): return _unauth()
    guild = _guild()
    if not guild:
        return jsonify({"channels": []})
    channels = [
        {"id": str(c.id), "name": c.name, "members": len(c.members)}
        for c in sorted(guild.voice_channels, key=lambda c: c.name)
    ]
    return jsonify({"channels": channels})


# API: MUSIC PLAY FROM DASHBOARD
@app.route("/api/music/play", methods=["POST"])
def api_music_play():
    if not _logged_in(): return _unauth()
    data             = request.json or {}
    query            = data.get("query", "").strip()
    voice_channel_id = data.get("voice_channel_id", "").strip()
    if not query:
        return jsonify({"error": "Query cannot be empty"}), 400
    try:
        vcid = int(voice_channel_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Select a voice channel"}), 400

    search_fn     = _state.get("search_youtube_fn")
    play_next_fn  = _state.get("play_next_fn")
    song_entry_cls= _state.get("song_entry_cls")
    music_states  = _state.get("music_states", {})
    guild_id      = _state.get("guild_id")

    if not (search_fn and play_next_fn and song_entry_cls):
        return jsonify({"error": "Music engine not ready yet — wait for bot to fully start"}), 503

    async def _do():
        guild = _guild()
        results = await search_fn(query, max_results=1)
        if not results:
            raise ValueError("No results found")
        r = results[0]
        # Get or create music state
        state = music_states.setdefault(guild_id, _state.get("get_music_state_fn")(guild_id))
        vc = guild.voice_client
        if vc is None:
            vc_ch = guild.get_channel(vcid)
            if not vc_ch:
                raise ValueError("Voice channel not found")
            vc = await vc_ch.connect()
            state.voice_client = vc
        elif vc.channel.id != vcid:
            vc_ch = guild.get_channel(vcid)
            await vc.move_to(vc_ch)
        bot_user = _state["client"].user
        entry = song_entry_cls(
            title=r.get("title", "Unknown"),
            url=r["url"],
            webpage_url=r.get("webpage_url", ""),
            duration=r.get("duration", 0),
            requester=bot_user,
        )
        state.queue.append(entry)
        if not vc.is_playing() and not vc.is_paused():
            bot_loop = _state.get("bot_loop")
            play_next_fn(guild_id, bot_loop)
            return f"Now playing: {entry.title}"
        return f"Queued: {entry.title} (position {len(state.queue)})"

    try:
        msg = _run(_do(), timeout=20)
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# API: MUSIC LEAVE
@app.route("/api/music/leave", methods=["POST"])
def api_music_leave():
    if not _logged_in(): return _unauth()
    music_states = _state.get("music_states", {})
    guild_id     = _state.get("guild_id")
    async def _do():
        guild = _guild()
        vc = guild.voice_client
        if not vc:
            raise ValueError("Bot is not in a voice channel")
        if guild_id in music_states:
            music_states[guild_id].queue.clear()
            music_states[guild_id].current = None
        await vc.disconnect()
    try:
        _run(_do())
        return jsonify({"ok": True, "message": "Disconnected from voice channel"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# API: LFG
@app.route("/api/lfg", methods=["POST"])
def api_lfg():
    if not _logged_in(): return _unauth()
    data       = request.json or {}
    channel_id = data.get("channel_id", "").strip()
    game       = data.get("game", "").strip()
    players    = int(data.get("players", 2))
    message    = data.get("message", "").strip()
    if not game:
        return jsonify({"error": "Game name is required"}), 400
    try:
        cid = int(channel_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid channel"}), 400
    async def _do():
        guild = _guild()
        ch = guild.get_channel(cid)
        if not ch:
            raise ValueError("Channel not found")
        embed = discord.Embed(
            title=f"🎮 Looking For Group — {game}",
            description=message or f"Looking for {players} players to join!",
            color=0x5865F2,
        )
        embed.add_field(name="Game", value=game, inline=True)
        embed.add_field(name="Players Needed", value=str(players), inline=True)
        embed.set_footer(text="Posted via Dashboard")
        embed.timestamp = discord.utils.utcnow()
        await ch.send(embed=embed)
    try:
        _run(_do())
        return jsonify({"ok": True, "message": f"LFG posted for {game}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# API: RANK LOOKUP
@app.route("/api/rank")
def api_rank():
    if not _logged_in(): return _unauth()
    uid_str  = request.args.get("user_id", "").strip()
    xp_data  = _state.get("xp_data", {})
    guild_id = _state.get("guild_id")
    if not uid_str:
        return jsonify({"error": "user_id required"}), 400
    try:
        uid = int(uid_str)
    except ValueError:
        return jsonify({"error": "Invalid user ID"}), 400
    xp    = xp_data.get(guild_id, {}).get(uid, 0)
    level = _xp_to_level(xp)
    next_level_xp = _xp_required(level + 1)
    current_level_xp = sum(_xp_required(l + 1) for l in range(level))
    progress_xp = xp - current_level_xp
    return jsonify({
        "name":  _member_name(uid),
        "level": level,
        "xp":    xp,
        "progress": progress_xp,
        "next_level_xp": next_level_xp,
    })

# ── PokeCoins management ──────────────────────────────────────────────────────
@app.route("/api/pokecoins/give", methods=["POST"])
def api_pokecoins_give():
    if not _logged_in(): return _unauth()
    data    = request.json or {}
    uid_str = str(data.get("user_id", "")).strip()
    amount  = data.get("amount", 0)
    if not uid_str:
        return jsonify({"error": "user_id required"}), 400
    try:
        uid    = int(uid_str)
        amount = int(amount)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid user_id or amount"}), 400
    if amount == 0:
        return jsonify({"error": "Amount cannot be zero"}), 400
    if abs(amount) > 1_000_000:
        return jsonify({"error": "Amount exceeds limit of 1,000,000"}), 400
    try:
        from pokemon_game import WALLETS, _ensure_player
        _ensure_player(uid)
        current = WALLETS.get(uid, 0)
        WALLETS[uid] = max(0, current + amount)
        action = "Gave" if amount > 0 else "Removed"
        return jsonify({
            "ok": True,
            "message": f"{action} {abs(amount):,} PokeCoins {'to' if amount > 0 else 'from'} {_member_name(uid)}",
            "new_balance": WALLETS[uid],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pokecoins/balance")
def api_pokecoins_balance():
    if not _logged_in(): return _unauth()
    uid_str = request.args.get("user_id", "").strip()
    if not uid_str:
        return jsonify({"error": "user_id required"}), 400
    try:
        uid = int(uid_str)
    except ValueError:
        return jsonify({"error": "Invalid user_id"}), 400
    try:
        from pokemon_game import WALLETS, _ensure_player
        _ensure_player(uid)
        return jsonify({
            "name":    _member_name(uid),
            "user_id": uid,
            "balance": WALLETS.get(uid, 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pokecoins/leaderboard")
def api_pokecoins_leaderboard():
    if not _logged_in(): return _unauth()
    try:
        from pokemon_game import WALLETS
        client   = _state.get("client")
        guild_id = _state.get("guild_id")
        guild    = client.get_guild(guild_id) if client else None
        member_ids = {m.id for m in guild.members} if guild else set()
        entries = [
            {"user_id": uid, "name": _member_name(uid), "balance": bal}
            for uid, bal in WALLETS.items()
            if not member_ids or uid in member_ids
        ]
        entries.sort(key=lambda x: x["balance"], reverse=True)
        return jsonify(entries[:50])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
