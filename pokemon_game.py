"""
Pokemon-style battle mini game for Discord.
Adds a /pokemon command group with: battle, accept, decline, attack, forfeit, moves.
"""

import random
import json
import os
import discord
from discord import app_commands
from discord.ext import commands
import io
import urllib.request

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None

# ── Pokemon Roster ────────────────────────────────────────────────────────────
POKEMON_ROSTER = [
    {"name": "Charizard",  "hp": 150, "atk": 85, "spd": 90,  "type": "Fire",     "emoji": "🔥", "moves": ["Flamethrower", "Dragon Claw", "Air Slash", "Fire Spin"],    "price": 800,  "rarity": "Rare"},
    {"name": "Blastoise",  "hp": 158, "atk": 75, "spd": 65,  "type": "Water",    "emoji": "💧", "moves": ["Hydro Pump", "Ice Beam", "Surf", "Bite"],                   "price": 800,  "rarity": "Rare"},
    {"name": "Venusaur",   "hp": 160, "atk": 75, "spd": 65,  "type": "Grass",    "emoji": "🌿", "moves": ["Solar Beam", "Razor Leaf", "Sludge Bomb", "Vine Whip"],      "price": 800,  "rarity": "Rare"},
    {"name": "Pikachu",    "hp": 110, "atk": 95, "spd": 110, "type": "Electric", "emoji": "⚡", "moves": ["Thunderbolt", "Quick Attack", "Iron Tail", "Thunder"],        "price": 300,  "rarity": "Common"},
    {"name": "Mewtwo",     "hp": 165, "atk": 110,"spd": 100, "type": "Psychic",  "emoji": "🔮", "moves": ["Psystrike", "Shadow Ball", "Aura Sphere", "Psychic"],          "price": 2500, "rarity": "Legendary"},
    {"name": "Gengar",     "hp": 120, "atk": 100,"spd": 95,  "type": "Ghost",    "emoji": "👻", "moves": ["Shadow Ball", "Dark Pulse", "Sludge Wave", "Hex"],             "price": 600,  "rarity": "Uncommon"},
    {"name": "Dragonite",  "hp": 155, "atk": 105,"spd": 80,  "type": "Dragon",   "emoji": "🐉", "moves": ["Dragon Rush", "Hurricane", "Fire Punch", "Outrage"],           "price": 1500, "rarity": "Epic"},
    {"name": "Lucario",    "hp": 130, "atk": 105,"spd": 90,  "type": "Fighting", "emoji": "🥊", "moves": ["Aura Sphere", "Close Combat", "Flash Cannon", "Bullet Punch"], "price": 1000, "rarity": "Rare"},
    {"name": "Garchomp",   "hp": 155, "atk": 108,"spd": 95,  "type": "Dragon",   "emoji": "🦷", "moves": ["Dragon Claw", "Earthquake", "Stone Edge", "Crunch"],           "price": 1500, "rarity": "Epic"},
    {"name": "Alakazam",   "hp": 105, "atk": 105,"spd": 105, "type": "Psychic",  "emoji": "🧠", "moves": ["Psychic", "Shadow Ball", "Focus Blast", "Dazzling Gleam"],     "price": 700,  "rarity": "Uncommon"},
    {"name": "Snorlax",    "hp": 200, "atk": 80, "spd": 30,  "type": "Normal",   "emoji": "😴", "moves": ["Body Slam", "Crunch", "Ice Punch", "Hyper Beam"],             "price": 500,  "rarity": "Common"},
    {"name": "Eevee",      "hp": 100, "atk": 65, "spd": 80,  "type": "Normal",   "emoji": "🦊", "moves": ["Quick Attack", "Bite", "Hyper Beam", "Iron Tail"],             "price": 200,  "rarity": "Common"},
]
# Fast lookup by name
POKEMON_BY_NAME: dict[str, dict] = {p["name"].lower(): p for p in POKEMON_ROSTER}

RAIRTY_COLORS = {"Common": 0xAAAAAA, "Uncommon": 0x2ECC71, "Rare": 0x3498DB, "Epic": 0x9B59B6, "Legendary": 0xFFD700}
RAIRTY_EMOJI  = {"Common": "⚪", "Uncommon": "🟢", "Rare": "🔵", "Epic": "🟣", "Legendary": "🌟"}

# ── Economy State ─────────────────────────────────────────────────────────────
# All keyed by user_id (int)
WALLETS:        dict[int, int]       = {}   # user_id -> PokeCoin balance
OWNED_POKEMON:  dict[int, list[str]] = {}   # user_id -> list of pokemon names owned
ACTIVE_POKEMON: dict[int, str]       = {}   # user_id -> active pokemon name
DAILY_CLAIMED:  dict[int, float]     = {}   # user_id -> last claim timestamp

STARTER_COINS   = 500    # coins every new player starts with
DAILY_COINS     = 200    # coins from /pokemon daily
WIN_COINS       = 150    # coins awarded for winning a battle
LOSE_COINS      = 30     # coins consolation for losing
STARTER_POKEMON = "Eevee"  # free pokemon every new player gets
POKEMON_CHANNEL_NAME = "pokemon-battle"
_MANAGED_CHANNELS_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "managed_channels.json")


def _tracked_channel_id(guild_id: int, key: str) -> int | None:
    try:
        with open(_MANAGED_CHANNELS_SAVE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    mapping = data.get(str(guild_id), {}) if isinstance(data, dict) else {}
    value = mapping.get(key)
    return int(value) if value else None


def _resolve_text_channel(guild: discord.Guild, key: str, *names: str) -> discord.TextChannel | None:
    tracked_id = _tracked_channel_id(guild.id, key)
    if tracked_id:
        tracked = guild.get_channel(tracked_id)
        if isinstance(tracked, discord.TextChannel):
            return tracked
    wanted = {name.casefold() for name in names if name}
    for channel in guild.text_channels:
        if channel.name.casefold() in wanted:
            return channel
    return None


async def _require_pokemon_channel(interaction: discord.Interaction) -> bool:
    channel = _resolve_text_channel(interaction.guild, "pokemon_channel", POKEMON_CHANNEL_NAME, "pokemon")
    if interaction.channel_id == (channel.id if channel else -1):
        return True
    mention = channel.mention if channel else f"#{POKEMON_CHANNEL_NAME}"
    await interaction.response.send_message(
        f"Pokemon commands can only be used in {mention}.",
        ephemeral=True,
    )
    return False


def _wallet(user_id: int) -> int:
    return WALLETS.get(user_id, 0)


def _ensure_player(user_id: int) -> None:
    """Give a new player their starter coins and pokemon on first interaction."""
    if user_id not in WALLETS:
        WALLETS[user_id] = STARTER_COINS
        OWNED_POKEMON.setdefault(user_id, [])
        if STARTER_POKEMON not in OWNED_POKEMON[user_id]:
            OWNED_POKEMON[user_id].append(STARTER_POKEMON)
        ACTIVE_POKEMON[user_id] = STARTER_POKEMON


def _get_active(user_id: int) -> dict:
    """Return the active pokemon data dict for a user."""
    _ensure_player(user_id)
    name = ACTIVE_POKEMON.get(user_id, STARTER_POKEMON)
    return POKEMON_BY_NAME[name.lower()]


# ── Move Data ─────────────────────────────────────────────────────────────────
MOVES = {
    # Fire
    "Flamethrower":   {"power": 90,  "acc": 100, "type": "Fire",     "effect": "burn"},
    "Fire Spin":      {"power": 35,  "acc": 85,  "type": "Fire",     "effect": None},
    "Fire Punch":     {"power": 75,  "acc": 100, "type": "Fire",     "effect": "burn"},
    # Water
    "Hydro Pump":     {"power": 110, "acc": 80,  "type": "Water",    "effect": None},
    "Surf":           {"power": 90,  "acc": 100, "type": "Water",    "effect": None},
    "Ice Beam":       {"power": 90,  "acc": 100, "type": "Ice",      "effect": "freeze"},
    "Ice Punch":      {"power": 75,  "acc": 100, "type": "Ice",      "effect": "freeze"},
    # Grass
    "Solar Beam":     {"power": 120, "acc": 100, "type": "Grass",    "effect": None},
    "Razor Leaf":     {"power": 55,  "acc": 95,  "type": "Grass",    "effect": "crit"},
    "Vine Whip":      {"power": 45,  "acc": 100, "type": "Grass",    "effect": None},
    "Sludge Bomb":    {"power": 90,  "acc": 100, "type": "Poison",   "effect": "poison"},
    "Sludge Wave":    {"power": 95,  "acc": 100, "type": "Poison",   "effect": "poison"},
    # Electric
    "Thunderbolt":    {"power": 90,  "acc": 100, "type": "Electric", "effect": None},
    "Thunder":        {"power": 110, "acc": 70,  "type": "Electric", "effect": "paralyze"},
    "Quick Attack":   {"power": 40,  "acc": 100, "type": "Normal",   "effect": None},
    "Iron Tail":      {"power": 100, "acc": 75,  "type": "Steel",    "effect": None},
    # Psychic
    "Psystrike":      {"power": 100, "acc": 100, "type": "Psychic",  "effect": None},
    "Psychic":        {"power": 90,  "acc": 100, "type": "Psychic",  "effect": None},
    "Aura Sphere":    {"power": 80,  "acc": 100, "type": "Fighting", "effect": None},
    "Focus Blast":    {"power": 120, "acc": 70,  "type": "Fighting", "effect": None},
    # Ghost / Dark
    "Shadow Ball":    {"power": 80,  "acc": 100, "type": "Ghost",    "effect": None},
    "Dark Pulse":     {"power": 80,  "acc": 100, "type": "Dark",     "effect": None},
    "Hex":            {"power": 65,  "acc": 100, "type": "Ghost",    "effect": None},
    # Dragon
    "Dragon Rush":    {"power": 100, "acc": 75,  "type": "Dragon",   "effect": None},
    "Dragon Claw":    {"power": 80,  "acc": 100, "type": "Dragon",   "effect": None},
    "Outrage":        {"power": 120, "acc": 100, "type": "Dragon",   "effect": None},
    # Fighting
    "Close Combat":   {"power": 120, "acc": 100, "type": "Fighting", "effect": "def_down"},
    "Bullet Punch":   {"power": 40,  "acc": 100, "type": "Steel",    "effect": None},
    # Ground / Rock
    "Earthquake":     {"power": 100, "acc": 100, "type": "Ground",   "effect": None},
    "Stone Edge":     {"power": 100, "acc": 80,  "type": "Rock",     "effect": "crit"},
    # Normal
    "Body Slam":      {"power": 85,  "acc": 100, "type": "Normal",   "effect": "paralyze"},
    "Hyper Beam":     {"power": 150, "acc": 90,  "type": "Normal",   "effect": None},
    # Other
    "Air Slash":      {"power": 75,  "acc": 95,  "type": "Flying",   "effect": None},
    "Bite":           {"power": 60,  "acc": 100, "type": "Dark",     "effect": None},
    "Crunch":         {"power": 80,  "acc": 100, "type": "Dark",     "effect": "def_down"},
    "Flash Cannon":   {"power": 80,  "acc": 100, "type": "Steel",    "effect": None},
    "Hurricane":      {"power": 110, "acc": 70,  "type": "Flying",   "effect": None},
    "Dazzling Gleam": {"power": 80,  "acc": 100, "type": "Fairy",    "effect": None},
}

# ── Type Effectiveness ────────────────────────────────────────────────────────
TYPE_CHART: dict[str, dict[str, float]] = {
    "Fire":     {"Grass": 2.0, "Ice": 2.0, "Steel": 2.0, "Bug": 2.0,
                 "Fire": 0.5, "Water": 0.5, "Rock": 0.5, "Dragon": 0.5},
    "Water":    {"Fire": 2.0, "Ground": 2.0, "Rock": 2.0,
                 "Water": 0.5, "Grass": 0.5, "Dragon": 0.5},
    "Grass":    {"Water": 2.0, "Ground": 2.0, "Rock": 2.0,
                 "Fire": 0.5, "Grass": 0.5, "Poison": 0.5,
                 "Flying": 0.5, "Bug": 0.5, "Dragon": 0.5, "Steel": 0.5},
    "Electric": {"Water": 2.0, "Flying": 2.0,
                 "Electric": 0.5, "Grass": 0.5, "Dragon": 0.5, "Ground": 0.0},
    "Psychic":  {"Fighting": 2.0, "Poison": 2.0,
                 "Psychic": 0.5, "Steel": 0.5, "Dark": 0.0},
    "Ghost":    {"Psychic": 2.0, "Ghost": 2.0,
                 "Dark": 0.5, "Normal": 0.0},
    "Dragon":   {"Dragon": 2.0, "Steel": 0.5, "Fairy": 0.0},
    "Fighting": {"Normal": 2.0, "Ice": 2.0, "Rock": 2.0, "Steel": 2.0, "Dark": 2.0,
                 "Flying": 0.5, "Psychic": 0.5, "Bug": 0.5,
                 "Poison": 0.5, "Fairy": 0.5, "Ghost": 0.0},
    "Poison":   {"Grass": 2.0, "Fairy": 2.0,
                 "Poison": 0.5, "Ground": 0.5, "Rock": 0.5,
                 "Ghost": 0.5, "Steel": 0.0},
    "Ground":   {"Fire": 2.0, "Electric": 2.0, "Poison": 2.0, "Rock": 2.0, "Steel": 2.0,
                 "Grass": 0.5, "Bug": 0.5, "Flying": 0.0},
    "Rock":     {"Fire": 2.0, "Ice": 2.0, "Flying": 2.0, "Bug": 2.0,
                 "Fighting": 0.5, "Ground": 0.5, "Steel": 0.5},
    "Ice":      {"Grass": 2.0, "Ground": 2.0, "Flying": 2.0, "Dragon": 2.0,
                 "Fire": 0.5, "Water": 0.5, "Ice": 0.5, "Steel": 0.5},
    "Dark":     {"Psychic": 2.0, "Ghost": 2.0,
                 "Fighting": 0.5, "Dark": 0.5, "Fairy": 0.5},
    "Steel":    {"Ice": 2.0, "Rock": 2.0, "Fairy": 2.0,
                 "Fire": 0.5, "Water": 0.5, "Electric": 0.5, "Steel": 0.5},
    "Flying":   {"Grass": 2.0, "Fighting": 2.0, "Bug": 2.0,
                 "Electric": 0.5, "Rock": 0.5, "Steel": 0.5},
    "Fairy":    {"Fighting": 2.0, "Dragon": 2.0, "Dark": 2.0,
                 "Fire": 0.5, "Poison": 0.5, "Steel": 0.5},
    "Normal":   {"Ghost": 0.0},
}

# ── Battle State ──────────────────────────────────────────────────────────────
# battles[channel_id] = battle dict
BATTLES: dict[int, dict] = {}
# challenges[challenger_id] = {opponent_id, channel_id}
CHALLENGES: dict[int, dict] = {}

import time as _time

# ── Gym + Badge System (Phase 3 retention) ─────────────────────────────────
# Gyms led by strong AI trainers; defeating grants a badge
GYMS = {
    "fire_gym": {"name": "Blaze Citadel", "leader": "Charizard", "badge": "🔥", "reward_coins": 500, "level": 30},
    "water_gym": {"name": "Aqua Palace", "leader": "Blastoise", "badge": "💧", "reward_coins": 500, "level": 30},
    "grass_gym": {"name": "Verdant Grove", "leader": "Venusaur", "badge": "🌿", "reward_coins": 500, "level": 30},
    "electric_gym": {"name": "Thunder Station", "leader": "Pikachu", "badge": "⚡", "reward_coins": 500, "level": 28},
    "psychic_gym": {"name": "Mind Palace", "leader": "Alakazam", "badge": "🔮", "reward_coins": 600, "level": 32},
    "dragon_gym": {"name": "Dragon's Peak", "leader": "Dragonite", "badge": "🐉", "reward_coins": 750, "level": 35},
}
GYM_BADGES: dict[int, set[str]] = {}  # user_id -> {gym_ids}

# ── Raid System (Phase 3 retention) ────────────────────────────────────────
# Group battles against a powerful raid boss
RAID_BOSS = {"name": "Mewtwo", "hp": 1000, "level": 45, "reward_coins": 2000}
ACTIVE_RAIDS: dict[int, dict] = {}  # raid_id -> {channel_id, boss_hp, team_members, started_at}
RAID_ID_COUNTER = 0

# ── Seasonal Ladder (Phase 3 retention) ───────────────────────────────────
# Track seasonal rankings; reset each month
import datetime as _dt
SEASON_START = _dt.datetime.now()
SEASON_BATTLES_WON: dict[int, int] = {}  # user_id -> wins this season
SEASON_RANK_POINTS: dict[int, int] = {}  # user_id -> ELO/rank points

# ── Type Colors ──────────────────────────────────────────────────────────────
TYPE_COLORS = {
    "Fire":     (238, 129, 48),      # Orange
    "Water":    (63, 131, 248),      # Blue
    "Grass":    (120, 200, 80),      # Green
    "Electric": (248, 208, 48),      # Yellow
    "Psychic":  (248, 88, 136),      # Pink
    "Ghost":    (112, 88, 152),      # Purple
    "Dragon":   (112, 56, 248),      # Dark Purple
    "Fighting": (200, 48, 48),       # Red
    "Poison":   (160, 64, 160),      # Magenta
    "Ground":   (224, 192, 104),     # Brown
    "Rock":     (184, 160, 56),      # Gray-Brown
    "Ice":      (152, 216, 216),     # Cyan
    "Dark":     (112, 88, 72),       # Dark Gray
    "Steel":    (184, 184, 208),     # Silver
    "Flying":   (168, 144, 240),     # Light Purple
    "Fairy":    (238, 154, 172),     # Light Pink
    "Normal":   (168, 168, 120),     # Gray
}

POKEDEX_IDS = {
    "Charizard": 6,
    "Blastoise": 9,
    "Venusaur": 3,
    "Pikachu": 25,
    "Mewtwo": 150,
    "Gengar": 94,
    "Dragonite": 149,
    "Lucario": 448,
    "Garchomp": 445,
    "Alakazam": 65,
    "Snorlax": 143,
    "Eevee": 133,
}

SPRITE_CACHE: dict[str, Image.Image | None] = {}
AVATAR_CACHE: dict[str, Image.Image | None] = {}


def _load_font(size: int = 24, bold: bool = False) -> ImageFont.FreeTypeFont | None:
    """Load a font at the given size, with DejaVu fallback."""
    if not ImageFont:
        return None
    candidates = [
        "c:/windows/fonts/arial.ttf",
        "c:/windows/fonts/arialbd.ttf" if bold else "c:/windows/fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return None


def _save_battle_file(img: Image.Image, filename: str) -> discord.File | None:
    """Convert PIL image to a Discord file."""
    if not img:
        return None
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return discord.File(bio, filename=filename)


def _load_pokemon_sprite(name: str) -> Image.Image | None:
    """Fetch and cache official Pokemon artwork by roster name."""
    if not Image:
        return None

    if name in SPRITE_CACHE:
        cached = SPRITE_CACHE[name]
        return cached.copy() if cached else None

    dex_id = POKEDEX_IDS.get(name)
    if not dex_id:
        SPRITE_CACHE[name] = None
        return None

    url = f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/{dex_id}.png"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            data = response.read()
        sprite = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        sprite = None

    SPRITE_CACHE[name] = sprite
    return sprite.copy() if sprite else None


def _load_remote_image(url: str | None) -> Image.Image | None:
    """Fetch and cache a remote image URL as RGBA."""
    if not Image or not url:
        return None

    if url in AVATAR_CACHE:
        cached = AVATAR_CACHE[url]
        return cached.copy() if cached else None

    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            data = response.read()
        remote = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        remote = None

    AVATAR_CACHE[url] = remote
    return remote.copy() if remote else None


def _paste_battle_sprite(
    img: Image.Image,
    sprite: Image.Image,
    *,
    center_x: int,
    baseline_y: int,
    max_w: int,
    max_h: int,
    flip: bool = False,
) -> None:
    """Paste a sprite scaled to fit an arena slot while preserving transparency."""
    if not sprite:
        return
    if flip and ImageOps:
        sprite = ImageOps.mirror(sprite)

    scale = min(max_w / sprite.width, max_h / sprite.height)
    target_w = max(1, int(sprite.width * scale))
    target_h = max(1, int(sprite.height * scale))
    resized = sprite.resize((target_w, target_h), Image.Resampling.LANCZOS)

    x = center_x - target_w // 2
    y = baseline_y - target_h
    img.alpha_composite(resized, (x, y))


def _paste_round_portrait(
    img: Image.Image,
    portrait: Image.Image,
    *,
    center_x: int,
    center_y: int,
    size: int,
    ring_color: tuple[int, int, int, int],
) -> None:
    """Paste a circular portrait with a colored ring."""
    if not portrait:
        return

    scaled = portrait.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size - 1, size - 1), fill=255)

    clipped = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    clipped.paste(scaled, (0, 0), mask)
    img.alpha_composite(clipped, (center_x - size // 2, center_y - size // 2))

    ring = ImageDraw.Draw(img)
    ring.ellipse(
        (center_x - size // 2 - 3, center_y - size // 2 - 3, center_x + size // 2 + 2, center_y + size // 2 + 2),
        outline=ring_color,
        width=4,
    )


def _pokemon_battle_render(battle: dict) -> discord.File | None:
    """Render a 960x600 battle scene with both Pokemon, HP bars, types, and statuses."""
    if not Image or not ImageDraw:
        return None

    width, height = 960, 600
    img = Image.new("RGBA", (width, height), (10, 16, 26, 255))
    draw = ImageDraw.Draw(img)
    font_name = _load_font(28, bold=True)
    font_hp = _load_font(18, bold=False)
    font_status = _load_font(14, bold=False)
    font_small = _load_font(12, bold=True)
    font_turn = _load_font(21, bold=True)

    p1, p2 = battle["p1"], battle["p2"]
    p1_name, p2_name = battle["p1_name"], battle["p2_name"]
    p1_avatar = _load_remote_image(battle.get("p1_avatar_url"))
    p2_avatar = _load_remote_image(battle.get("p2_avatar_url"))
    fx = battle.pop("_fx", None)
    p1_type_color = TYPE_COLORS.get(p1["type"], (128, 128, 128))
    p2_type_color = TYPE_COLORS.get(p2["type"], (128, 128, 128))

    # Vertical sky gradient + subtle scanlines for a premium arena look.
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(14 + (38 - 14) * t)
        g = int(20 + (42 - 20) * t)
        b = int(34 + (66 - 34) * t)
        draw.line((0, y, width, y), fill=(r, g, b, 255), width=1)
        if y % 4 == 0:
            draw.line((0, y, width, y), fill=(255, 255, 255, 10), width=1)

    # Corner color washes based on each Pokemon type.
    for i in range(9):
        alpha = max(8, 70 - i * 7)
        pad = i * 18
        draw.ellipse((20 + pad, 40 + pad, 360 - pad, 380 - pad), fill=(*p1_type_color, alpha))
        draw.ellipse((600 + pad, 10 + pad, 940 - pad, 350 - pad), fill=(*p2_type_color, alpha))

    # Arena floor so sprites visibly appear on a battlefield.
    draw.ellipse((130, 302, 440, 468), fill=(64, 94, 72, 235), outline=(170, 214, 184, 255), width=3)
    draw.ellipse((520, 222, 830, 378), fill=(64, 94, 72, 235), outline=(170, 214, 184, 255), width=3)
    draw.ellipse((160, 330, 410, 450), fill=(20, 30, 26, 100))
    draw.ellipse((550, 250, 800, 360), fill=(20, 30, 26, 100))

    # Glass HUD panels on top of scene.
    draw.rounded_rectangle((24, 18, 412, 214), radius=18, fill=(12, 20, 36, 170), outline=(140, 170, 210, 150), width=2)
    draw.rounded_rectangle((548, 18, 936, 214), radius=18, fill=(12, 20, 36, 170), outline=(140, 170, 210, 150), width=2)

    # VS medallion + center split beam.
    draw.rectangle((475, 190, 485, 470), fill=(255, 255, 255, 22))
    draw.ellipse((424, 238, 536, 350), fill=(32, 40, 56, 220), outline=(255, 214, 128, 255), width=3)
    draw.text((455, 275), "VS", fill=(255, 220, 150, 255), font=_load_font(30, bold=True))

    p1_sprite = _load_pokemon_sprite(p1["name"])
    p2_sprite = _load_pokemon_sprite(p2["name"])
    if p1_sprite:
        # Warm sprite glow to reduce flatness on dark backgrounds.
        for i in range(4):
            draw.ellipse((208 - i * 5, 210 - i * 4, 362 + i * 5, 360 + i * 4), fill=(255, 240, 200, 20 - i * 3))
        _paste_battle_sprite(img, p1_sprite, center_x=285, baseline_y=332, max_w=230, max_h=230, flip=False)
    else:
        draw.text((250, 250), p1["emoji"], fill=(255, 255, 255, 255), font=_load_font(96, bold=True))

    if p2_sprite:
        for i in range(4):
            draw.ellipse((598 - i * 5, 125 - i * 4, 752 + i * 5, 280 + i * 4), fill=(220, 240, 255, 20 - i * 3))
        _paste_battle_sprite(img, p2_sprite, center_x=675, baseline_y=250, max_w=210, max_h=210, flip=True)
    else:
        draw.text((640, 180), p2["emoji"], fill=(255, 255, 255, 255), font=_load_font(84, bold=True))

    # One-frame move effects (hit, miss, no-effect) on the battlefield.
    if fx:
        kind = fx.get("kind", "hit")
        side = fx.get("side")
        base_color = fx.get("color", (255, 96, 96))
        crit = bool(fx.get("crit"))
        cx, cy = (285, 260) if side == "p1" else (675, 185)
        if kind == "hit":
            burst_size = 180 if crit else 145
            for i in range(7):
                alpha = max(18, (140 if crit else 105) - i * 16)
                pad = i * 13
                draw.ellipse(
                    (cx - burst_size // 2 - pad, cy - burst_size // 2 - pad, cx + burst_size // 2 + pad, cy + burst_size // 2 + pad),
                    outline=(base_color[0], base_color[1], base_color[2], alpha),
                    width=3 if i < 3 else 2,
                )
            draw.line((cx - 70, cy, cx + 70, cy), fill=(255, 255, 255, 130), width=2)
            draw.line((cx, cy - 70, cx, cy + 70), fill=(255, 255, 255, 130), width=2)
            if crit:
                draw.text((cx - 28, cy - 104), "CRIT!", fill=(255, 235, 130, 255), font=_load_font(22, bold=True))
        elif kind == "miss":
            for i in range(4):
                pad = i * 8
                draw.arc((cx - 78 - pad, cy - 58 - pad, cx + 78 + pad, cy + 58 + pad), 205, 335, fill=(220, 220, 220, 140 - i * 22), width=3)
            draw.line((cx - 64, cy - 22, cx + 64, cy + 22), fill=(240, 240, 240, 150), width=3)
            draw.text((cx - 26, cy - 80), "MISS", fill=(230, 230, 230, 230), font=_load_font(18, bold=True))
        elif kind == "no_effect":
            for i in range(6):
                alpha = max(18, 120 - i * 17)
                pad = i * 11
                draw.rounded_rectangle(
                    (cx - 72 - pad, cy - 62 - pad, cx + 72 + pad, cy + 62 + pad),
                    radius=24,
                    outline=(140, 220, 255, alpha),
                    width=2,
                )
            draw.text((cx - 58, cy - 82), "NO EFFECT", fill=(170, 235, 255, 235), font=_load_font(15, bold=True))

    # Trainer portraits near each side of the arena.
    p1_active = battle["turn"] == "p1"
    p2_active = battle["turn"] == "p2"
    if p1_avatar:
        _paste_round_portrait(img, p1_avatar, center_x=100, center_y=305, size=82, ring_color=(80, 180, 255, 255))
        if p1_active:
            for i in range(4):
                draw.ellipse((56 - i * 5, 261 - i * 5, 144 + i * 5, 349 + i * 5), outline=(80, 200, 255, 130 - i * 28), width=2)
    if p2_avatar:
        _paste_round_portrait(img, p2_avatar, center_x=860, center_y=225, size=82, ring_color=(255, 150, 70, 255))
        if p2_active:
            for i in range(4):
                draw.ellipse((816 - i * 5, 181 - i * 5, 904 + i * 5, 269 + i * 5), outline=(255, 170, 90, 130 - i * 28), width=2)

    # ─── Left side: Player 1 ────────────────────────────────────────────────
    # Pokemon name + emoji
    draw.text((40, 30), f"{p1['emoji']} {p1['name']}", fill=(236, 236, 236, 255), font=font_name)
    # Trainer name
    draw.text((40, 65), p1_name, fill=(180, 180, 180, 255), font=_load_font(16))
    # Type badge
    type_color = p1_type_color + (255,)
    draw.rounded_rectangle((40, 95, 140, 120), radius=6, fill=type_color, outline=(200, 200, 200, 255), width=2)
    draw.text((50, 99), p1["type"], fill=(255, 255, 255, 255), font=font_small)
    # HP bar background
    draw.rounded_rectangle((40, 150, 380, 175), radius=6, fill=(60, 60, 60, 255), outline=(100, 100, 100, 255), width=2)
    # HP bar fill (green gradient)
    hp_ratio = max(0.0, p1["current_hp"] / p1["max_hp"])
    bar_width = int((380 - 40) * hp_ratio)
    bar_color = (76, 175, 80, 255) if hp_ratio > 0.5 else ((255, 193, 7, 255) if hp_ratio > 0.25 else (244, 67, 54, 255))
    if bar_width > 0:
        draw.rounded_rectangle((40, 150, 40 + bar_width, 175), radius=6, fill=bar_color)
    # HP text
    hp_text = f"{p1['current_hp']} / {p1['max_hp']}"
    draw.text((200, 152), hp_text, fill=(255, 255, 255, 255), font=font_hp)
    # Status condition
    if p1.get("status"):
        status_icon = {"poison": "☠️", "burn": "🔥", "freeze": "🧊", "paralysis": "⚡"}.get(p1["status"], "❓")
        draw.text((40, 195), f"Status: {status_icon} {p1['status'].upper()}", fill=(255, 150, 0, 255), font=font_status)

    # ─── Right side: Player 2 ───────────────────────────────────────────────
    # Pokemon name + emoji (right-aligned)
    p2_name_text = f"{p2['emoji']} {p2['name']}"
    p2_bbox = draw.textbbox((0, 0), p2_name_text, font=font_name)
    p2_name_x = 960 - (p2_bbox[2] - p2_bbox[0]) - 40
    draw.text((p2_name_x, 30), p2_name_text, fill=(236, 236, 236, 255), font=font_name)
    # Trainer name
    p2_trainer_bbox = draw.textbbox((0, 0), p2_name, font=_load_font(16))
    p2_trainer_x = 960 - (p2_trainer_bbox[2] - p2_trainer_bbox[0]) - 40
    draw.text((p2_trainer_x, 65), p2_name, fill=(180, 180, 180, 255), font=_load_font(16))
    # Type badge
    type_color_p2 = p2_type_color + (255,)
    draw.rounded_rectangle((820, 95, 920, 120), radius=6, fill=type_color_p2, outline=(200, 200, 200, 255), width=2)
    p2_type_bbox = draw.textbbox((0, 0), p2["type"], font=font_small)
    p2_type_x = 870 - (p2_type_bbox[2] - p2_type_bbox[0]) // 2
    draw.text((p2_type_x, 99), p2["type"], fill=(255, 255, 255, 255), font=font_small)
    # HP bar background
    draw.rounded_rectangle((580, 150, 920, 175), radius=6, fill=(60, 60, 60, 255), outline=(100, 100, 100, 255), width=2)
    # HP bar fill
    hp_ratio_p2 = max(0.0, p2["current_hp"] / p2["max_hp"])
    bar_width_p2 = int((920 - 580) * hp_ratio_p2)
    bar_color_p2 = (76, 175, 80, 255) if hp_ratio_p2 > 0.5 else ((255, 193, 7, 255) if hp_ratio_p2 > 0.25 else (244, 67, 54, 255))
    if bar_width_p2 > 0:
        draw.rounded_rectangle((920 - bar_width_p2, 150, 920, 175), radius=6, fill=bar_color_p2)
    # HP text (right-aligned)
    hp_text_p2 = f"{p2['current_hp']} / {p2['max_hp']}"
    hp_p2_bbox = draw.textbbox((0, 0), hp_text_p2, font=font_hp)
    hp_p2_x = 750 - (hp_p2_bbox[2] - hp_p2_bbox[0]) // 2
    draw.text((hp_p2_x, 152), hp_text_p2, fill=(255, 255, 255, 255), font=font_hp)
    # Status condition
    if p2.get("status"):
        status_icon = {"poison": "☠️", "burn": "🔥", "freeze": "🧊", "paralysis": "⚡"}.get(p2["status"], "❓")
        status_text = f"Status: {status_icon} {p2['status'].upper()}"
        status_bbox = draw.textbbox((0, 0), status_text, font=font_status)
        status_x = 920 - (status_bbox[2] - status_bbox[0]) - 40
        draw.text((status_x, 195), status_text, fill=(255, 150, 0, 255), font=font_status)

    # ─── VS separator ───────────────────────────────────────────────────────
    draw.text((405, 360), "⚔️  Arena Clash  ⚔️", fill=(255, 215, 150, 210), font=_load_font(24, bold=True))

    # ─── Turn indicator (bottom) ─────────────────────────────────────────────
    current_trainer = battle["p1_name"] if battle["turn"] == "p1" else battle["p2_name"]
    turn_text = f"🎮 {current_trainer}'s Turn"
    draw.rounded_rectangle((280, 506, 680, 560), radius=16, fill=(10, 14, 24, 185), outline=(255, 220, 130, 200), width=2)
    turn_bbox = draw.textbbox((0, 0), turn_text, font=font_turn)
    turn_x = (960 - (turn_bbox[2] - turn_bbox[0])) // 2
    draw.text((turn_x, 522), turn_text, fill=(255, 211, 94, 255), font=font_turn)

    return _save_battle_file(img, "pokemon_battle.png")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _type_mult(move_type: str, defender_type: str) -> float:
    return TYPE_CHART.get(move_type, {}).get(defender_type, 1.0)


def _hp_bar(current: int, maximum: int, length: int = 12) -> str:
    ratio = max(0.0, current / maximum)
    filled = round(ratio * length)
    empty = length - filled
    if ratio > 0.5:
        color = "🟩"
    elif ratio > 0.25:
        color = "🟨"
    else:
        color = "🟥"
    return color * filled + "⬛" * empty


def _battle_embed(battle: dict, title: str, description: str, color: int = 0x2b2d31) -> discord.Embed:
    p1, p2 = battle["p1"], battle["p2"]
    embed = discord.Embed(title=title, description=description, color=color)

    # ── Fighter 1 ──
    bar1 = _hp_bar(p1["current_hp"], p1["max_hp"])
    st1 = f"  `{p1['status'].upper()}`" if p1.get("status") else ""
    embed.add_field(
        name=f"{p1['emoji']} {p1['name']} [{p1['type']}]{st1}",
        value=f"{bar1}\n`{p1['current_hp']} / {p1['max_hp']} HP`\n*{battle['p1_name']}*",
        inline=True,
    )

    embed.add_field(name="\u200b", value="**⚔️ VS ⚔️**", inline=True)

    # ── Fighter 2 ──
    bar2 = _hp_bar(p2["current_hp"], p2["max_hp"])
    st2 = f"  `{p2['status'].upper()}`" if p2.get("status") else ""
    embed.add_field(
        name=f"{p2['emoji']} {p2['name']} [{p2['type']}]{st2}",
        value=f"{bar2}\n`{p2['current_hp']} / {p2['max_hp']} HP`\n*{battle['p2_name']}*",
        inline=True,
    )

    # ── Whose turn ──
    cur = p1 if battle["turn"] == "p1" else p2
    cur_trainer = battle["p1_name"] if battle["turn"] == "p1" else battle["p2_name"]
    moves_fmt = "  |  ".join(f"`{m}`" for m in cur["moves"])
    embed.add_field(
        name=f"🎮 {cur_trainer}'s Turn — {cur['emoji']} {cur['name']}",
        value=f"Use `/pokemon attack` and pick a move:\n{moves_fmt}",
        inline=False,
    )
    embed.set_footer(text="Use /pokemon forfeit to give up  •  /pokemon moves for move details")
    return embed


def _calculate_damage(attacker: dict, defender: dict, move_name: str) -> tuple[int, str, bool]:
    """Returns (damage, effectiveness_label, is_crit). damage=0 means miss."""
    move = MOVES[move_name]

    # Miss check
    if random.randint(1, 100) > move["acc"]:
        return 0, "missed", False

    # Crit — 10% base, always crit for "crit" effect moves
    is_crit = move.get("effect") == "crit" or random.randint(1, 10) == 1

    power = move["power"]
    atk = attacker["atk"]
    def_mod = max(0.5, defender.get("def_mod", 1.0))  # def_mod > 1 = weaker defense

    base = int((power * atk / 100) * random.uniform(0.85, 1.0) / def_mod)

    mult = _type_mult(move["type"], defender["type"])
    damage = int(base * mult)
    if is_crit:
        damage = int(damage * 1.5)

    if mult >= 2.0:
        label = "super effective"
    elif mult == 0.0:
        label = "no effect"
    elif mult < 1.0:
        label = "not very effective"
    else:
        label = "normal"

    return max(1, damage), label, is_crit


def _apply_effect(target: dict, effect: str | None) -> str | None:
    """Apply move secondary effect. Returns flavour text or None."""
    if not effect or target.get("status"):
        return None
    if effect == "poison" and random.random() < 0.30:
        target["status"] = "poison"
        return f"☠️ **{target['name']}** was **poisoned**!"
    if effect == "paralyze" and random.random() < 0.30:
        target["status"] = "paralysis"
        return f"⚡ **{target['name']}** is **paralyzed**!"
    if effect == "burn" and random.random() < 0.30:
        target["status"] = "burn"
        return f"🔥 **{target['name']}** was **burned**!"
    if effect == "freeze" and random.random() < 0.20:
        target["status"] = "freeze"
        return f"🧊 **{target['name']}** was **frozen solid**!"
    if effect == "def_down":
        target["def_mod"] = target.get("def_mod", 1.0) * 1.33
        return f"📉 **{target['name']}'s** Defense fell!"
    return None


def _tick_status(pokemon: dict) -> tuple[int, str | None, bool]:
    """
    Process start-of-turn status.
    Returns (damage, message, move_blocked).
    """
    status = pokemon.get("status")
    if status == "poison":
        dmg = max(1, int(pokemon["max_hp"] * 0.0625))
        return dmg, f"☠️ **{pokemon['name']}** is hurt by **poison**! (-{dmg} HP)", False
    if status == "burn":
        dmg = max(1, int(pokemon["max_hp"] * 0.0625))
        return dmg, f"🔥 **{pokemon['name']}** is hurt by its **burn**! (-{dmg} HP)", False
    if status == "freeze":
        if random.random() < 0.25:
            pokemon["status"] = None
            return 0, f"🌡️ **{pokemon['name']}** thawed out!", False
        return 0, f"🧊 **{pokemon['name']}** is **frozen solid** and can't move!", True
    if status == "paralysis":
        if random.random() < 0.25:
            return 0, f"⚡ **{pokemon['name']}** is **paralyzed** and can't move!", True
    return 0, None, False


def _make_fighter(data: dict) -> dict:
    return {
        "name":       data["name"],
        "emoji":      data["emoji"],
        "type":       data["type"],
        "max_hp":     data["hp"],
        "current_hp": data["hp"],
        "atk":        data["atk"],
        "spd":        data["spd"],
        "moves":      list(data["moves"]),
        "status":     None,
        "def_mod":    1.0,
    }


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_pokemon(bot: commands.Bot) -> None:
    """Register the /pokemon command group on the bot tree."""

    pokemon_group = app_commands.Group(
        name="pokemon",
        description="⚔️ Pokemon-style turn-based battle mini game",
    )

    raid_group = app_commands.Group(
        name="raid",
        description="🦹 Team up for raid boss battles",
    )

    # ── /pokemon battle ───────────────────────────────────────────────────────
    @pokemon_group.command(name="battle", description="Challenge another member to a Pokemon battle!")
    @app_commands.describe(opponent="The member you want to challenge")
    async def cmd_battle(interaction: discord.Interaction, opponent: discord.Member):
        if not await _require_pokemon_channel(interaction):
            return
        if opponent.bot:
            await interaction.response.send_message("❌ You can't challenge a bot!", ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message("❌ You can't challenge yourself!", ephemeral=True)
            return
        channel_id = interaction.channel_id
        if channel_id in BATTLES:
            await interaction.response.send_message("❌ A battle is already active in this channel!", ephemeral=True)
            return
        CHALLENGES[interaction.user.id] = {
            "opponent_id": opponent.id,
            "channel_id":  channel_id,
        }
        embed = discord.Embed(
            title="⚔️ Pokemon Battle Challenge!",
            description=(
                f"{interaction.user.mention} challenges {opponent.mention} to a **Pokemon Battle**!\n\n"
                f"{opponent.mention} — use `/pokemon accept` to fight or `/pokemon decline` to back down.\n\n"
                f"*Challenge expires when the challenger starts a new one.*"
            ),
            color=0xFF6B35,
        )
        embed.set_footer(text="⚡ Only one can win!")
        await interaction.response.send_message(embed=embed)

    # ── /pokemon accept ───────────────────────────────────────────────────────
    @pokemon_group.command(name="accept", description="Accept a Pokemon battle challenge directed at you")
    async def cmd_accept(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        user_id    = interaction.user.id
        channel_id = interaction.channel_id

        challenger_id = None
        for cid, data in CHALLENGES.items():
            if data["opponent_id"] == user_id and data["channel_id"] == channel_id:
                challenger_id = cid
                break

        if challenger_id is None:
            await interaction.response.send_message(
                "❌ No pending challenge for you in this channel!", ephemeral=True
            )
            return

        del CHALLENGES[challenger_id]

        # Use each player's active Pokemon
        _ensure_player(challenger_id)
        _ensure_player(user_id)
        p1 = _make_fighter(_get_active(challenger_id))
        p2 = _make_fighter(_get_active(user_id))

        # Faster goes first
        first_turn = "p1" if p1["spd"] >= p2["spd"] else "p2"

        challenger_user = await interaction.client.fetch_user(challenger_id)

        battle = {
            "p1":       p1,
            "p2":       p2,
            "p1_id":    challenger_id,
            "p2_id":    user_id,
            "p1_name":  challenger_user.display_name,
            "p2_name":  interaction.user.display_name,
            "p1_avatar_url": str(challenger_user.display_avatar.url),
            "p2_avatar_url": str(interaction.user.display_avatar.url),
            "turn":     first_turn,
            "channel_id": channel_id,
        }
        BATTLES[channel_id] = battle

        first_name  = battle["p1_name"] if first_turn == "p1" else battle["p2_name"]
        first_poke  = p1 if first_turn == "p1" else p2

        embed = _battle_embed(
            battle,
            "⚔️ Battle Start!",
            (
                f"**{battle['p1_name']}** sends out **{p1['emoji']} {p1['name']}**!\n"
                f"**{battle['p2_name']}** sends out **{p2['emoji']} {p2['name']}**!\n\n"
                f"⚡ **{first_name}** goes first with **{first_poke['emoji']} {first_poke['name']}** (higher Speed)!"
            ),
            color=0x00FF88,
        )
        battle_file = _pokemon_battle_render(battle)
        embed.set_image(url="attachment://pokemon_battle.png") if battle_file else None
        await interaction.response.send_message(embed=embed, file=battle_file)

    # ── /pokemon decline ──────────────────────────────────────────────────────
    @pokemon_group.command(name="decline", description="Decline a Pokemon battle challenge")
    async def cmd_decline(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        user_id    = interaction.user.id
        channel_id = interaction.channel_id

        challenger_id = None
        for cid, data in CHALLENGES.items():
            if data["opponent_id"] == user_id and data["channel_id"] == channel_id:
                challenger_id = cid
                break

        if challenger_id is None:
            await interaction.response.send_message("❌ No pending challenge to decline!", ephemeral=True)
            return

        del CHALLENGES[challenger_id]
        challenger = await interaction.client.fetch_user(challenger_id)
        await interaction.response.send_message(
            f"❌ {interaction.user.mention} declined {challenger.mention}'s battle challenge."
        )

    # ── /pokemon attack ───────────────────────────────────────────────────────
    @pokemon_group.command(name="attack", description="Use a move during your Pokemon battle turn")
    @app_commands.describe(move="The move to use")
    async def cmd_attack(interaction: discord.Interaction, move: str):
        if not await _require_pokemon_channel(interaction):
            return
        channel_id = interaction.channel_id
        if channel_id not in BATTLES:
            await interaction.response.send_message("❌ No active battle in this channel!", ephemeral=True)
            return

        battle  = BATTLES[channel_id]
        user_id = interaction.user.id
        battle.pop("_fx", None)

        # Validate turn
        if battle["turn"] == "p1" and user_id != battle["p1_id"]:
            await interaction.response.send_message(
                f"❌ It's **{battle['p1_name']}'s** turn, not yours!", ephemeral=True
            )
            return
        if battle["turn"] == "p2" and user_id != battle["p2_id"]:
            await interaction.response.send_message(
                f"❌ It's **{battle['p2_name']}'s** turn, not yours!", ephemeral=True
            )
            return

        if battle["turn"] == "p1":
            attacker, defender        = battle["p1"], battle["p2"]
            attacker_name, next_turn  = battle["p1_name"], "p2"
            attacker_side, defender_side = "p1", "p2"
        else:
            attacker, defender        = battle["p2"], battle["p1"]
            attacker_name, next_turn  = battle["p2_name"], "p1"
            attacker_side, defender_side = "p2", "p1"

        # Validate move
        move_map = {m.lower(): m for m in attacker["moves"]}
        if move.lower() not in move_map:
            moves_list = "  |  ".join(f"`{m}`" for m in attacker["moves"])
            await interaction.response.send_message(
                f"❌ **{attacker['name']}** doesn't know that move!\nAvailable: {moves_list}",
                ephemeral=True,
            )
            return

        actual_move = move_map[move.lower()]
        lines: list[str] = []

        # ── Status tick ──
        status_dmg, status_msg, blocked = _tick_status(attacker)
        if status_msg:
            lines.append(status_msg)
        if status_dmg > 0:
            attacker["current_hp"] = max(0, attacker["current_hp"] - status_dmg)

        # ── Attack (unless blocked by status) ──
        if not blocked:
            damage, effectiveness, is_crit = _calculate_damage(attacker, defender, actual_move)
            move_type = MOVES.get(actual_move, {}).get("type", "Normal")
            fx_color = TYPE_COLORS.get(move_type, (255, 96, 96))

            if effectiveness == "missed":
                lines.append(f"{attacker['emoji']} **{attacker['name']}** used **{actual_move}**... but it **missed**!")
                battle["_fx"] = {
                    "side": attacker_side,
                    "kind": "miss",
                    "color": (210, 210, 210),
                    "crit": False,
                }
            elif effectiveness == "no effect":
                lines.append(f"{attacker['emoji']} **{attacker['name']}** used **{actual_move}**... it has **no effect**!")
                battle["_fx"] = {
                    "side": defender_side,
                    "kind": "no_effect",
                    "color": (140, 220, 255),
                    "crit": False,
                }
            else:
                defender["current_hp"] = max(0, defender["current_hp"] - damage)
                battle["_fx"] = {
                    "side": defender_side,
                    "kind": "hit",
                    "color": fx_color,
                    "crit": is_crit,
                }

                crit_txt = "  💥 **Critical hit!**" if is_crit else ""
                eff_txt  = ""
                if effectiveness == "super effective":
                    eff_txt = "  🔥 **Super effective!**"
                elif effectiveness == "not very effective":
                    eff_txt = "  😑 *Not very effective...*"

                lines.append(
                    f"{attacker['emoji']} **{attacker['name']}** used **{actual_move}**! "
                    f"(-**{damage}** HP){crit_txt}{eff_txt}"
                )

                effect_msg = _apply_effect(defender, MOVES[actual_move].get("effect"))
                if effect_msg:
                    lines.append(effect_msg)

        # ── Check: defender fainted ──
        if defender["current_hp"] <= 0:
            del BATTLES[channel_id]
            lines.append(f"\n💀 **{defender['emoji']} {defender['name']}** fainted!")
            embed = _battle_embed(
                battle,
                "🏆 Battle Over!",
                "\n".join(lines) + f"\n\n🎉 **{attacker_name}** wins the battle!",
                color=0xFFD700,
            )
            embed.remove_field(3)  # remove the "whose turn" field
            embed.set_footer(text=f"GG! +{WIN_COINS} PokeCoins to the winner.")
            # Award coins
            winner_id = battle["p1_id"] if battle["turn"] == "p1" else battle["p2_id"]
            loser_id  = battle["p2_id"] if battle["turn"] == "p1" else battle["p1_id"]
            WALLETS[winner_id] = _wallet(winner_id) + WIN_COINS
            WALLETS[loser_id]  = _wallet(loser_id)  + LOSE_COINS
            battle_file = _pokemon_battle_render(battle)
            embed.set_image(url="attachment://pokemon_battle.png") if battle_file else None
            await interaction.response.send_message(embed=embed, file=battle_file)
            return

        # ── Check: attacker fainted (status tick damage) ──
        if attacker["current_hp"] <= 0:
            del BATTLES[channel_id]
            winner_name = battle["p2_name"] if battle["turn"] == "p1" else battle["p1_name"]
            lines.append(f"\n💀 **{attacker['emoji']} {attacker['name']}** fainted from status damage!")
            embed = _battle_embed(
                battle,
                "🏆 Battle Over!",
                "\n".join(lines) + f"\n\n🎉 **{winner_name}** wins the battle!",
                color=0xFFD700,
            )
            embed.remove_field(3)
            embed.set_footer(text=f"GG! +{WIN_COINS} PokeCoins to the winner.")
            winner_id = battle["p2_id"] if battle["turn"] == "p1" else battle["p1_id"]
            loser_id  = battle["p1_id"] if battle["turn"] == "p1" else battle["p2_id"]
            WALLETS[winner_id] = _wallet(winner_id) + WIN_COINS
            WALLETS[loser_id]  = _wallet(loser_id)  + LOSE_COINS
            battle_file = _pokemon_battle_render(battle)
            embed.set_image(url="attachment://pokemon_battle.png") if battle_file else None
            await interaction.response.send_message(embed=embed, file=battle_file)
            return

        # ── Continue battle ──
        battle["turn"] = next_turn
        embed = _battle_embed(battle, "⚔️ Pokemon Battle", "\n".join(lines) or "\u200b", color=0x5865F2)
        battle_file = _pokemon_battle_render(battle)
        embed.set_image(url="attachment://pokemon_battle.png") if battle_file else None
        await interaction.response.send_message(embed=embed, file=battle_file)

    # ── Autocomplete for /pokemon attack ─────────────────────────────────────
    @cmd_attack.autocomplete("move")
    async def attack_autocomplete(interaction: discord.Interaction, current: str):
        channel_id = interaction.channel_id
        if channel_id not in BATTLES:
            return []
        battle  = BATTLES[channel_id]
        user_id = interaction.user.id
        if battle["turn"] == "p1" and user_id == battle["p1_id"]:
            moves = battle["p1"]["moves"]
        elif battle["turn"] == "p2" and user_id == battle["p2_id"]:
            moves = battle["p2"]["moves"]
        else:
            return []
        return [
            app_commands.Choice(name=m, value=m)
            for m in moves if current.lower() in m.lower()
        ]

    # ── /pokemon forfeit ──────────────────────────────────────────────────────
    @pokemon_group.command(name="forfeit", description="Forfeit your current Pokemon battle")
    async def cmd_forfeit(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        channel_id = interaction.channel_id
        if channel_id not in BATTLES:
            await interaction.response.send_message("❌ No active battle in this channel!", ephemeral=True)
            return
        battle  = BATTLES[channel_id]
        user_id = interaction.user.id
        if user_id not in (battle["p1_id"], battle["p2_id"]):
            await interaction.response.send_message("❌ You're not part of this battle!", ephemeral=True)
            return
        if user_id == battle["p1_id"]:
            loser, winner = battle["p1_name"], battle["p2_name"]
        else:
            loser, winner = battle["p2_name"], battle["p1_name"]
        del BATTLES[channel_id]
        embed = discord.Embed(
            title="🏳️ Battle Forfeited",
            description=f"**{loser}** forfeited the battle!\n🏆 **{winner}** wins by default!",
            color=0xFF4444,
        )
        await interaction.response.send_message(embed=embed)

    # ── /pokemon moves ────────────────────────────────────────────────────────
    @pokemon_group.command(name="moves", description="View move details for the current battle")
    async def cmd_moves(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        channel_id = interaction.channel_id
        if channel_id not in BATTLES:
            await interaction.response.send_message("❌ No active battle in this channel!", ephemeral=True)
            return
        battle = BATTLES[channel_id]
        p1, p2 = battle["p1"], battle["p2"]

        def move_line(m: str) -> str:
            d = MOVES[m]
            eff = f"  *(effect: {d['effect']})*" if d.get("effect") else ""
            return f"`{m}` — Pwr **{d['power']}** | Acc **{d['acc']}%** | Type **{d['type']}**{eff}"

        embed = discord.Embed(title="📖 Move Reference", color=0x7289DA)
        embed.add_field(
            name=f"{p1['emoji']} {p1['name']} ({battle['p1_name']})",
            value="\n".join(move_line(m) for m in p1["moves"]),
            inline=False,
        )
        embed.add_field(
            name=f"{p2['emoji']} {p2['name']} ({battle['p2_name']})",
            value="\n".join(move_line(m) for m in p2["moves"]),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _raid_embed(raid_data: dict) -> discord.Embed:
        team_members = raid_data.get("team_members", [])
        ready_members = raid_data.get("ready_members", [])
        embed = discord.Embed(
            title="🦹 **RAID BOSS APPEARED!**",
            description=(
                f"**{RAID_BOSS['name']}** (Lv. {RAID_BOSS['level']}) has appeared!\n\n"
                f"**HP:** `{raid_data['boss_hp']:,} / {raid_data['max_hp']:,}`\n"
                f"**Reward:** `{RAID_BOSS['reward_coins']:,} PokeCoins` per member"
            ),
            color=0xFF4444,
        )
        if team_members:
            embed.add_field(
                name=f"⚔️ Team Members ({len(team_members)})",
                value="\n".join(f"<@{uid}>" for uid in team_members),
                inline=False,
            )
        else:
            embed.add_field(name="⚔️ Team Members (0)", value="No one joined yet.", inline=False)

        if ready_members:
            embed.add_field(
                name=f"✅ Ready ({len(ready_members)}/{len(team_members) if team_members else 0})",
                value="\n".join(f"<@{uid}>" for uid in ready_members),
                inline=False,
            )

        embed.add_field(
            name="📋 Controls",
            value="Use the buttons below: **Join**, **Ready**, **Leave**.",
            inline=False,
        )
        embed.set_footer(text=f"Raid ID: {raid_data['raid_id']}")
        return embed

    class RaidBossView(discord.ui.View):
        def __init__(self, raid_id: int):
            super().__init__(timeout=300)
            self.raid_id = raid_id

        def _get_raid(self) -> dict | None:
            return ACTIVE_RAIDS.get(self.raid_id)

        async def _refresh(self, interaction: discord.Interaction):
            raid = self._get_raid()
            if raid is None:
                for item in self.children:
                    item.disabled = True
                if interaction.response.is_done():
                    await interaction.edit_original_response(content="❌ This raid is no longer active.", view=self)
                else:
                    await interaction.response.edit_message(content="❌ This raid is no longer active.", view=self)
                return
            embed = _raid_embed(raid)
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)

        async def on_timeout(self):
            raid = self._get_raid()
            if raid is not None:
                ACTIVE_RAIDS.pop(self.raid_id, None)
            for item in self.children:
                item.disabled = True

        @discord.ui.button(label="Join", emoji="🎯", style=discord.ButtonStyle.success)
        async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
            raid = self._get_raid()
            if raid is None:
                await interaction.response.send_message("❌ This raid is no longer active.", ephemeral=True)
                return
            user_id = interaction.user.id
            _ensure_player(user_id)
            if user_id not in raid["team_members"]:
                raid["team_members"].append(user_id)
            if user_id in raid["ready_members"]:
                raid["ready_members"].remove(user_id)
            await self._refresh(interaction)

        @discord.ui.button(label="Ready", emoji="⏱️", style=discord.ButtonStyle.primary)
        async def ready(self, interaction: discord.Interaction, button: discord.ui.Button):
            raid = self._get_raid()
            if raid is None:
                await interaction.response.send_message("❌ This raid is no longer active.", ephemeral=True)
                return
            user_id = interaction.user.id
            if user_id not in raid["team_members"]:
                await interaction.response.send_message("❌ Join the raid first using **Join**.", ephemeral=True)
                return
            if user_id not in raid["ready_members"]:
                raid["ready_members"].append(user_id)
            raid["ready"] = len(raid["team_members"]) > 0 and len(raid["ready_members"]) == len(raid["team_members"])
            await self._refresh(interaction)

        @discord.ui.button(label="Leave", emoji="🛑", style=discord.ButtonStyle.danger)
        async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
            raid = self._get_raid()
            if raid is None:
                await interaction.response.send_message("❌ This raid is no longer active.", ephemeral=True)
                return
            user_id = interaction.user.id
            if user_id in raid["team_members"]:
                raid["team_members"].remove(user_id)
            if user_id in raid["ready_members"]:
                raid["ready_members"].remove(user_id)
            raid["ready"] = len(raid["team_members"]) > 0 and len(raid["ready_members"]) == len(raid["team_members"])
            if not raid["team_members"]:
                ACTIVE_RAIDS.pop(self.raid_id, None)
                for item in self.children:
                    item.disabled = True
                if interaction.response.is_done():
                    await interaction.edit_original_response(content="❌ Raid canceled (no team members left).", view=self)
                else:
                    await interaction.response.edit_message(content="❌ Raid canceled (no team members left).", view=self)
                return
            await self._refresh(interaction)

    async def _start_raid_boss(interaction: discord.Interaction):
        import time
        global RAID_ID_COUNTER

        if not await _require_pokemon_channel(interaction):
            return

        _ensure_player(interaction.user.id)
        channel_id = interaction.channel_id

        existing_raid = next((r for r in ACTIVE_RAIDS.values() if r.get("channel_id") == channel_id), None)
        if existing_raid:
            await interaction.response.send_message(
                "❌ A raid is already active in this channel! Wait for it to finish.",
                ephemeral=True,
            )
            return

        raid_id = RAID_ID_COUNTER
        RAID_ID_COUNTER += 1

        boss_hp = RAID_BOSS["hp"]
        raid_data = {
            "raid_id": raid_id,
            "channel_id": channel_id,
            "boss_hp": boss_hp,
            "max_hp": boss_hp,
            "team_members": [interaction.user.id],
            "ready_members": [],
            "started_at": time.time(),
            "ready": False,
            "round": 0,
        }
        ACTIVE_RAIDS[raid_id] = raid_data

        view = RaidBossView(raid_id)
        await interaction.response.send_message(embed=_raid_embed(raid_data), view=view)

    # ── /pokemon boss ─────────────────────────────────────────────────────────
    @pokemon_group.command(name="boss", description="🦹 Challenge the legendary Raid Boss Mewtwo as a team!")
    async def cmd_pokemon_boss(interaction: discord.Interaction):
        await _start_raid_boss(interaction)

    # ── /raid boss ────────────────────────────────────────────────────────────
    @raid_group.command(name="boss", description="🦹 Challenge the legendary Raid Boss Mewtwo as a team!")
    async def cmd_raid_boss(interaction: discord.Interaction):
        await _start_raid_boss(interaction)

    # Register the group globally
    bot.tree.add_command(pokemon_group)
    bot.tree.add_command(raid_group)


def setup_pokemon_economy(bot: commands.Bot) -> None:
    """Register /pokeshop, /pokedex, /pokepick, /pokewallet, /pokebuy, /pokdaily commands."""

    # ── /pokewallet ───────────────────────────────────────────────────────────
    @bot.tree.command(name="pokewallet", description="Check your PokeCoin balance and active Pokemon")
    async def cmd_pokewallet(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        uid = interaction.user.id
        _ensure_player(uid)
        balance = _wallet(uid)
        active  = ACTIVE_POKEMON.get(uid, STARTER_POKEMON)
        owned   = OWNED_POKEMON.get(uid, [])
        pdata   = POKEMON_BY_NAME.get(active.lower())
        embed = discord.Embed(
            title=f"👛 {interaction.user.display_name}'s Trainer Card",
            color=0xFFD700,
        )
        embed.add_field(name="💰 PokeCoins", value=f"**{balance:,}**", inline=True)
        embed.add_field(name="⚡ Active Pokemon",
                        value=f"{pdata['emoji']} **{active}**" if pdata else active,
                        inline=True)
        embed.add_field(name="📦 Pokemon Owned", value=str(len(owned)), inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="Use /pokeshop to buy Pokemon  •  /pokepick to switch active")
        await interaction.response.send_message(embed=embed)

    # ── /pokdaily ─────────────────────────────────────────────────────────────
    @bot.tree.command(name="pokdaily", description="Claim your daily PokeCoin reward")
    async def cmd_pokdaily(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        uid = interaction.user.id
        _ensure_player(uid)
        now  = _time.time()
        last = DAILY_CLAIMED.get(uid, 0)
        diff = now - last
        if diff < 86400:
            remaining = 86400 - diff
            h, m = int(remaining // 3600), int((remaining % 3600) // 60)
            await interaction.response.send_message(
                f"⏳ You already claimed today! Come back in **{h}h {m}m**.", ephemeral=True
            )
            return
        DAILY_CLAIMED[uid] = now
        WALLETS[uid] = _wallet(uid) + DAILY_COINS
        embed = discord.Embed(
            title="💰 Daily Reward Claimed!",
            description=f"You received **{DAILY_COINS} PokeCoins**!\n"
                        f"New balance: **{WALLETS[uid]:,} PokeCoins**",
            color=0x2ECC71,
        )
        embed.set_footer(text="Come back tomorrow for more!")
        await interaction.response.send_message(embed=embed)

    # ── /pokeshop ─────────────────────────────────────────────────────────────
    @bot.tree.command(name="pokeshop", description="Browse all Pokemon available for purchase")
    async def cmd_pokeshop(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        uid = interaction.user.id
        _ensure_player(uid)
        owned = OWNED_POKEMON.get(uid, [])
        lines: dict[str, list[str]] = {}
        for p in POKEMON_ROSTER:
            r = p["rarity"]
            re = RAIRTY_EMOJI[r]
            owned_tag = " ✅" if p["name"] in owned else ""
            line = f"{p['emoji']} **{p['name']}**{owned_tag} — `{p['price']:,}` coins"
            lines.setdefault(r, []).append(line)
        embed = discord.Embed(
            title="🛒 Pokemon Shop",
            description=f"Your balance: **{_wallet(uid):,} PokeCoins**\n\nUse `/pokebuy <name>` to purchase!",
            color=0xFF6B35,
        )
        order = ["Common", "Uncommon", "Rare", "Epic", "Legendary"]
        for r in order:
            if r in lines:
                embed.add_field(
                    name=f"{RAIRTY_EMOJI[r]} {r}",
                    value="\n".join(lines[r]),
                    inline=False,
                )
        embed.set_footer(text="✅ = already owned")
        await interaction.response.send_message(embed=embed)

    # ── /pokebuy ──────────────────────────────────────────────────────────────
    @bot.tree.command(name="pokebuy", description="Buy a Pokemon from the shop")
    @app_commands.describe(pokemon="The Pokemon you want to buy")
    async def cmd_pokebuy(interaction: discord.Interaction, pokemon: str):
        if not await _require_pokemon_channel(interaction):
            return
        uid = interaction.user.id
        _ensure_player(uid)
        pdata = POKEMON_BY_NAME.get(pokemon.lower())
        if pdata is None:
            await interaction.response.send_message(
                f"❌ **{pokemon}** isn't in the shop! Check `/pokeshop` for valid names.",
                ephemeral=True,
            )
            return
        owned = OWNED_POKEMON.setdefault(uid, [])
        if pdata["name"] in owned:
            await interaction.response.send_message(
                f"❌ You already own **{pdata['emoji']} {pdata['name']}**!", ephemeral=True
            )
            return
        price = pdata["price"]
        if _wallet(uid) < price:
            await interaction.response.send_message(
                f"❌ Not enough PokeCoins! You need **{price:,}** but have **{_wallet(uid):,}**.",
                ephemeral=True,
            )
            return
        WALLETS[uid] -= price
        owned.append(pdata["name"])
        embed = discord.Embed(
            title="🎉 Pokemon Purchased!",
            description=(
                f"You bought **{pdata['emoji']} {pdata['name']}** "
                f"({RAIRTY_EMOJI[pdata['rarity']]} {pdata['rarity']})!\n\n"
                f"Remaining balance: **{WALLETS[uid]:,} PokeCoins**\n\n"
                f"Use `/pokepick {pdata['name']}` to make it your active Pokemon!"
            ),
            color=RAIRTY_COLORS[pdata["rarity"]],
        )
        await interaction.response.send_message(embed=embed)

    @cmd_pokebuy.autocomplete("pokemon")
    async def pokebuy_autocomplete(interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=f"{p['emoji']} {p['name']} ({p['rarity']}) — {p['price']} coins", value=p["name"])
            for p in POKEMON_ROSTER if current.lower() in p["name"].lower()
        ][:25]

    # ── /pokepick ─────────────────────────────────────────────────────────────
    @bot.tree.command(name="pokepick", description="Switch your active Pokemon to one you own")
    @app_commands.describe(pokemon="The Pokemon to set as active")
    async def cmd_pokepick(interaction: discord.Interaction, pokemon: str):
        if not await _require_pokemon_channel(interaction):
            return
        uid = interaction.user.id
        _ensure_player(uid)
        pdata = POKEMON_BY_NAME.get(pokemon.lower())
        if pdata is None:
            await interaction.response.send_message(
                f"❌ **{pokemon}** doesn't exist! Check `/pokeshop`.", ephemeral=True
            )
            return
        if pdata["name"] not in OWNED_POKEMON.get(uid, []):
            await interaction.response.send_message(
                f"❌ You don't own **{pdata['emoji']} {pdata['name']}**! Buy it first with `/pokebuy`.",
                ephemeral=True,
            )
            return
        ACTIVE_POKEMON[uid] = pdata["name"]
        embed = discord.Embed(
            title="✅ Active Pokemon Updated!",
            description=f"Your active Pokemon is now **{pdata['emoji']} {pdata['name']}**!\n"
                        f"It will be used in your next battle.",
            color=RAIRTY_COLORS[pdata["rarity"]],
        )
        await interaction.response.send_message(embed=embed)

    @cmd_pokepick.autocomplete("pokemon")
    async def pokepick_autocomplete(interaction: discord.Interaction, current: str):
        uid  = interaction.user.id
        owned = OWNED_POKEMON.get(uid, [])
        return [
            app_commands.Choice(name=f"{POKEMON_BY_NAME[n.lower()]['emoji']} {n}", value=n)
            for n in owned if current.lower() in n.lower()
        ][:25]

    # ── /pokedex ──────────────────────────────────────────────────────────────
    @bot.tree.command(name="pokedex", description="View all Pokemon you own")
    async def cmd_pokedex(interaction: discord.Interaction):
        if not await _require_pokemon_channel(interaction):
            return
        uid = interaction.user.id
        _ensure_player(uid)
        owned  = OWNED_POKEMON.get(uid, [])
        active = ACTIVE_POKEMON.get(uid, "")
        if not owned:
            await interaction.response.send_message("📦 You don't own any Pokemon yet! Use `/pokeshop`.", ephemeral=True)
            return
        lines: list[str] = []
        for name in owned:
            pdata = POKEMON_BY_NAME.get(name.lower())
            if not pdata:
                continue
            active_tag = " ⚡ **(Active)**" if name == active else ""
            lines.append(
                f"{pdata['emoji']} **{name}**{active_tag} — "
                f"{RAIRTY_EMOJI[pdata['rarity']]} {pdata['rarity']} | "
                f"HP {pdata['hp']} | ATK {pdata['atk']} | SPD {pdata['spd']}"
            )
        embed = discord.Embed(
            title=f"📖 {interaction.user.display_name}'s Pokedex",
            description="\n".join(lines),
            color=0x3498DB,
        )
        embed.set_footer(text="Use /pokepick <name> to switch active Pokemon")
        await interaction.response.send_message(embed=embed)
