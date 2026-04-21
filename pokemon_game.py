"""
Pokemon-style battle mini game for Discord.
Adds a /pokemon command group with: battle, accept, decline, attack, forfeit, moves.
"""

import random
import discord
from discord import app_commands
from discord.ext import commands

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

def setup_pokemon(bot: commands.Bot, guild_id: int) -> None:
    """Register the /pokemon command group on the bot tree."""

    pokemon_group = app_commands.Group(
        name="pokemon",
        description="⚔️ Pokemon-style turn-based battle mini game",
    )

    # ── /pokemon battle ───────────────────────────────────────────────────────
    @pokemon_group.command(name="battle", description="Challenge another member to a Pokemon battle!")
    @app_commands.describe(opponent="The member you want to challenge")
    async def cmd_battle(interaction: discord.Interaction, opponent: discord.Member):
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
        await interaction.response.send_message(embed=embed)

    # ── /pokemon decline ──────────────────────────────────────────────────────
    @pokemon_group.command(name="decline", description="Decline a Pokemon battle challenge")
    async def cmd_decline(interaction: discord.Interaction):
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
        channel_id = interaction.channel_id
        if channel_id not in BATTLES:
            await interaction.response.send_message("❌ No active battle in this channel!", ephemeral=True)
            return

        battle  = BATTLES[channel_id]
        user_id = interaction.user.id

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
        else:
            attacker, defender        = battle["p2"], battle["p1"]
            attacker_name, next_turn  = battle["p2_name"], "p1"

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

            if effectiveness == "missed":
                lines.append(f"{attacker['emoji']} **{attacker['name']}** used **{actual_move}**... but it **missed**!")
            elif effectiveness == "no effect":
                lines.append(f"{attacker['emoji']} **{attacker['name']}** used **{actual_move}**... it has **no effect**!")
            else:
                defender["current_hp"] = max(0, defender["current_hp"] - damage)

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
            await interaction.response.send_message(embed=embed)
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
            await interaction.response.send_message(embed=embed)
            return

        # ── Continue battle ──
        battle["turn"] = next_turn
        embed = _battle_embed(battle, "⚔️ Pokemon Battle", "\n".join(lines) or "\u200b", color=0x5865F2)
        await interaction.response.send_message(embed=embed)

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

    # Register the group with the guild
    bot.tree.add_command(pokemon_group, guild=discord.Object(id=guild_id))


def setup_pokemon_economy(bot: commands.Bot, guild_id: int) -> None:
    """Register /pokeshop, /pokedex, /pokepick, /pokewallet, /pokebuy, /pokdaily commands."""
    gobj = discord.Object(id=guild_id)

    # ── /pokewallet ───────────────────────────────────────────────────────────
    @bot.tree.command(name="pokewallet", description="Check your PokeCoin balance and active Pokemon", guild=gobj)
    async def cmd_pokewallet(interaction: discord.Interaction):
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
    @bot.tree.command(name="pokdaily", description="Claim your daily PokeCoin reward", guild=gobj)
    async def cmd_pokdaily(interaction: discord.Interaction):
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
    @bot.tree.command(name="pokeshop", description="Browse all Pokemon available for purchase", guild=gobj)
    async def cmd_pokeshop(interaction: discord.Interaction):
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
    @bot.tree.command(name="pokebuy", description="Buy a Pokemon from the shop", guild=gobj)
    @app_commands.describe(pokemon="The Pokemon you want to buy")
    async def cmd_pokebuy(interaction: discord.Interaction, pokemon: str):
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
    @bot.tree.command(name="pokepick", description="Switch your active Pokemon to one you own", guild=gobj)
    @app_commands.describe(pokemon="The Pokemon to set as active")
    async def cmd_pokepick(interaction: discord.Interaction, pokemon: str):
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
    @bot.tree.command(name="pokedex", description="View all Pokemon you own", guild=gobj)
    async def cmd_pokedex(interaction: discord.Interaction):
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
