"""
gambling.py — Casino mini-games powered by PokeCoin currency.

Upgrades v2:
  • 7% chance of a 3× Bonus Round on any win across all games
  • Reel-by-reel slot animation (left → mid → right reveal)
  • Realistic slot box showing 3 rows (top/win-line/bottom)
  • Wheel slice display for roulette
  • ASCII dice faces for Dice Duel
  • NEW: /highlow  — guess if the next card is higher or lower (1.9× payout)
  • NEW: /plinko   — Plinko board with 9 buckets, up to 3× payout
  • NEW: /daily    — claim 200–600 free PokeCoins every 24 h
  • NEW: /setupcasino — admin creates a dedicated #🎰-casino-floor channel
  • Casino stats tracking (games, won, lost, biggest win, win/loss streak)
  • Bonus-round overlay on every game embed
"""

import random
import asyncio
import time
import io
import math
import discord
from discord import app_commands
from discord.ext import commands

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None

from pokemon_game import _wallet, _ensure_player, WALLETS

BOT_LOG_NAME = "🤖┃bot-logs"

# ── Config ────────────────────────────────────────────────────────────────────
MIN_BET      = 10
MAX_BET      = 50_000
BONUS_CHANCE = 0.07          # 7 % chance of 3× bonus on any win
DAILY_RANGE  = (200, 600)    # min / max free daily coins
WORK_RANGE   = (120, 420)    # min / max coins from /work
WORK_COOLDOWN_SECONDS = 3_600
HEIST_JOIN_SECONDS = 10
HEIST_MAX_PLAYERS = 4
HEIST_PLAYER_BONUS = 0.05
HEIST_PLAYER_BONUS_CAP = 0.15
HEIST_SUCCESS_CAP = 0.92

# ── Per-user casino stats ─────────────────────────────────────────────────────
# uid → {games, won, lost, biggest_win, streak, streak_type}
CASINO_STATS: dict[int, dict] = {}
_DAILY_CD:    dict[int, float] = {}   # uid → epoch of last /daily claim
_WORK_CD:     dict[int, float] = {}   # uid → epoch of last /work claim
_HEIST_BUSY_CHANNELS: set[int] = set()

_WORK_EVENTS = [
    "You refereed a ranked Pokemon battle and earned **{coins:,}** PokeCoins.",
    "You repaired the roulette wheel rails before tournament night and got paid **{coins:,}** PokeCoins.",
    "You polished a trainer's badges for the Hall display and made **{coins:,}** PokeCoins.",
    "You caddied for the Celadon Game Corner and took home **{coins:,}** PokeCoins.",
    "You delivered rare Pokeballs to the battle arena and earned **{coins:,}** PokeCoins.",
    "You worked the casino cashier desk during rush hour and made **{coins:,}** PokeCoins.",
    "You helped Nurse Joy restock battle supplies and received **{coins:,}** PokeCoins.",
    "You trained rookie trainers on type matchups and earned **{coins:,}** PokeCoins.",
    "You commentated a gym challenge stream and got tipped **{coins:,}** PokeCoins.",
    "You maintained the slots cabinet reels and were paid **{coins:,}** PokeCoins.",
]

_HEIST_TARGETS = {
    "store": {"name": "🏪 Corner Store",    "mult": 1.5,  "success_base": 0.75, "color": 0x2ECC71},
    "bank":  {"name": "🏦 City Bank",       "mult": 3.0,  "success_base": 0.50, "color": 0x3498DB},
    "vault": {"name": "💎 Diamond Vault",   "mult": 7.0,  "success_base": 0.30, "color": 0x9B59B6},
    "fed":   {"name": "🚀 Federal Reserve", "mult": 15.0, "success_base": 0.12, "color": 0xFF4444},
}

_HEIST_CREW_ROLES = [
    ("🔓 Safecracker", 0.10),
    ("🚗 Getaway Driver", 0.08),
    ("💻 Hacker", 0.12),
    ("🔫 Muscle", 0.05),
    ("🕵️ Inside Man", 0.15),
]

_HEIST_STAGES = [
    ("🚨 Disabling the alarm", "bypassed silently", "alarm triggered!"),
    ("📦 Cracking the vault", "cracked in seconds", "silent alert sent"),
    ("🏃 Loading the loot", "bags loaded, ready to go", "guard spotted movement"),
    ("🚗 Making the getaway", "vanished into the night", "police gave chase"),
]


def _stats(uid: int) -> dict:
    if uid not in CASINO_STATS:
        CASINO_STATS[uid] = {
            "games": 0, "won": 0, "lost": 0,
            "biggest_win": 0, "streak": 0, "streak_type": None,
        }
    return CASINO_STATS[uid]


def _record_win(uid: int, amount: int) -> None:
    s = _stats(uid)
    s["games"] += 1
    s["won"]   += amount
    s["biggest_win"] = max(s["biggest_win"], amount)
    if s["streak_type"] == "win":
        s["streak"] += 1
    else:
        s["streak"] = 1
        s["streak_type"] = "win"


def _record_loss(uid: int, amount: int) -> None:
    s = _stats(uid)
    s["games"] += 1
    s["lost"]  += amount
    if s["streak_type"] == "loss":
        s["streak"] += 1
    else:
        s["streak"] = 1
        s["streak_type"] = "loss"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _check_bet(uid: int, amount: int) -> str | None:
    _ensure_player(uid)
    if amount < MIN_BET:
        return f"❌ Minimum bet is **{MIN_BET:,} PokeCoins**."
    if amount > MAX_BET:
        return f"❌ Maximum bet is **{MAX_BET:,} PokeCoins**."
    if _wallet(uid) < amount:
        return (
            f"❌ Not enough PokeCoins! "
            f"You have **{_wallet(uid):,}** but need **{amount:,}**."
        )
    return None


class PlayAgainView(discord.ui.View):
    def __init__(self, uid: int, command_hint: str, replay_action=None):
        super().__init__(timeout=300)
        self.uid = uid
        self.command_hint = command_hint
        self.replay_action = replay_action

    @discord.ui.button(label="Play Again", emoji="🎮", style=discord.ButtonStyle.success)
    async def play_again(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ This play-again button isn't for your game.", ephemeral=True)
            return
        if self.replay_action is not None:
            try:
                await self.replay_action(interaction)
                return
            except Exception as e:
                print(f"[Casino] Play-again replay failed: {e}")
        await interaction.response.send_message(f"Run `/{self.command_hint}` to play again.", ephemeral=True)


def _bal_line(uid: int) -> str:
    return f"💼 **Balance:** `{_wallet(uid):,}` PokeCoins"


async def _log_coin_event(
    guild: discord.Guild,
    recipient: discord.abc.User,
    amount: int,
    source: str,
    actor: discord.abc.User | None = None,
    reason: str | None = None,
) -> None:
    """Log PokeCoin gains/losses to #mod-logs when available."""
    if guild is None or recipient is None or amount == 0:
        return
    log_ch = discord.utils.get(guild.text_channels, name=BOT_LOG_NAME)
    if not log_ch:
        log_ch = discord.utils.get(guild.text_channels, name="bot-logs")
    if not log_ch:
        log_ch = discord.utils.get(guild.text_channels, name="mod-logs")
    if not log_ch:
        return

    sign = "+" if amount > 0 else ""
    color = 0x2ECC71 if amount > 0 else 0xE67E22
    embed = discord.Embed(title="💰 PokeCoin Event", color=color)
    embed.add_field(name="Recipient", value=f"{recipient.mention} (`{recipient.id}`)", inline=True)
    embed.add_field(name="Amount", value=f"`{sign}{amount:,}` PokeCoins", inline=True)
    embed.add_field(name="Balance", value=f"`{_wallet(recipient.id):,}` PokeCoins", inline=True)
    embed.add_field(name="Source", value=source, inline=False)
    if actor is not None:
        embed.add_field(name="Actor", value=f"{actor.mention} (`{actor.id}`)", inline=True)
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    embed.timestamp = discord.utils.utcnow()
    await log_ch.send(embed=embed)


def _bonus_roll(uid: int, base_win: int) -> tuple[int, bool]:
    """Possibly apply a 3× Casino Bonus Round. Returns (final_payout, triggered)."""
    if random.random() < BONUS_CHANCE:
        return base_win * 3, True
    return base_win, False


# ══════════════════════════════════════════════════════════════════════════════
# 🎰  SLOT MACHINE
# ══════════════════════════════════════════════════════════════════════════════

_SLOT_REELS: list[tuple[str, int]] = [
    ("🍒",  2),
    ("🍋",  3),
    ("🍊",  4),
    ("🍇",  5),
    ("⭐",  8),
    ("💎", 15),
    ("7️⃣", 25),
    ("🎰", 50),
]
_SLOT_SYMBOLS = [s for s, _ in _SLOT_REELS]
_SLOT_WEIGHTS = [30, 25, 20, 15, 5, 3, 1.5, 0.5]
_SLOT_MULT    = {s: m for s, m in _SLOT_REELS}

# ── Basket system (prevents repeat spam) ──────────────────────────────────────
_USER_BASKETS: dict[int, list[str]] = {}


def _fill_basket() -> list[str]:
    pool: list[str] = []
    for sym, w in zip(_SLOT_SYMBOLS, _SLOT_WEIGHTS):
        pool.extend([sym] * max(int(w), 1))
    random.shuffle(pool)
    return pool


def _draw(uid: int) -> str:
    basket = _USER_BASKETS.get(uid)
    if not basket:
        basket = _fill_basket()
        _USER_BASKETS[uid] = basket
    return basket.pop()


def _slot_box(s1: str, s2: str, s3: str) -> str:
    """Full 3-row slot display (top / win-line / bottom)."""
    def rand_other(s: str) -> str:
        others = [x for x in _SLOT_SYMBOLS if x != s]
        return random.choice(others)

    t1, t2, t3 = rand_other(s1), rand_other(s2), rand_other(s3)
    b1, b2, b3 = rand_other(s1), rand_other(s2), rand_other(s3)
    return (
        "```\n"
        "╔════════════════════╗\n"
        f"║  {t1}    {t2}    {t3}   ║\n"
        "╠═══ WIN  LINE ══════╣\n"
        f"║▶ {s1}    {s2}    {s3}  ◀║\n"
        "╠════════════════════╣\n"
        f"║  {b1}    {b2}    {b3}   ║\n"
        "╚════════════════════╝\n"
        "```"
    )


def _slot_spin_box(revealed: list[str]) -> str:
    """Partial reel reveal — 🌀 for pending reels."""
    reels = list(revealed) + ["🌀"] * (3 - len(revealed))
    s1, s2, s3 = reels
    return (
        "```\n"
        "╔════════════════════╗\n"
        "║  ❓    ❓    ❓   ║\n"
        "╠═══ WIN  LINE ══════╣\n"
        f"║▶ {s1}    {s2}    {s3}  ◀║\n"
        "╠════════════════════╣\n"
        "║  ❓    ❓    ❓   ║\n"
        "╚════════════════════╝\n"
        "```"
    )


def _slots_embed(
    display: str, bet: int, uid: int,
    result_text: str = "", color: int = 0x5865F2,
    spinning: bool = False, bonus: bool = False,
) -> discord.Embed:
    title = "🎰 Slot Machine"
    if spinning:
        title += "  ·  🌀 Spinning..."
    elif bonus:
        title = "🎰  🌟 BONUS ROUND TRIGGERED! 🌟"
    embed = discord.Embed(title=title, color=0xFFD700 if bonus else color)
    embed.description = display
    embed.add_field(name="💰 Bet",     value=f"`{bet:,}` PokeCoins",       inline=True)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    if result_text:
        embed.add_field(name="📊 Result", value=result_text, inline=False)
    if bonus:
        embed.add_field(
            name="⚡ CASINO BONUS",
            value="🎊 **3× MULTIPLIER** activated — your winnings were tripled!",
            inline=False,
        )
    if not spinning:
        embed.set_footer(text="🎰 /slots  |  📅 /daily for free coins  |  📊 /casinomenu")
    return embed


def _slot_render_label(sym: str) -> str:
    # Keep symbols readable even if emoji glyph fallback is limited.
    labels = {
        "🍒": "CH",
        "🍋": "LE",
        "🍊": "OR",
        "🍇": "GR",
        "⭐": "ST",
        "💎": "DI",
        "7️⃣": "7",
        "🎰": "JP",
        "🌀": "..",
        "❓": "?",
    }
    return labels.get(sym, "?")


def _slots_render_image_file(revealed: list[str], spinning: bool = False) -> discord.File | None:
    if Image is None or ImageDraw is None or ImageFont is None:
        return None

    w, h = 980, 560
    img = Image.new("RGBA", (w, h), (7, 50, 39, 255))
    draw = ImageDraw.Draw(img)

    # Felt texture background.
    for y in range(0, h, 4):
        shade = 34 + ((y // 4) % 2) * 2
        draw.line((0, y, w, y), fill=(6, shade, 30, 255), width=2)
    draw.rounded_rectangle((14, 14, w - 14, h - 14), radius=36, outline=(84, 170, 128, 220), width=4)

    # Main slot cabinet frame.
    frame = (88, 52, w - 88, h - 68)
    draw.rounded_rectangle(frame, radius=30, fill=(22, 30, 36, 255), outline=(165, 126, 48, 255), width=6)
    draw.rounded_rectangle((102, 68, w - 102, h - 84), radius=24, fill=(18, 23, 28, 255), outline=(58, 68, 78, 255), width=2)

    # Reels window.
    win_box = (164, 134, w - 164, h - 148)
    draw.rounded_rectangle(win_box, radius=18, fill=(10, 13, 18, 255), outline=(74, 84, 96, 255), width=3)

    reels = list(revealed) + ["🌀"] * (3 - len(revealed))
    c1, c2, c3 = reels
    top = [random.choice(_SLOT_SYMBOLS) for _ in range(3)]
    bot = [random.choice(_SLOT_SYMBOLS) for _ in range(3)]

    # Font setup.
    try:
        font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 42)
        font_sym = ImageFont.truetype("DejaVuSans-Bold.ttf", 36)
        font_small = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
    except Exception:
        font_title = ImageFont.load_default()
        font_sym = ImageFont.load_default()
        font_small = ImageFont.load_default()

    draw.text((w // 2 - 136, 78), "SLOT MACHINE", fill=(242, 208, 109, 255), font=font_title)

    col_w = 190
    row_h = 112
    gap = 20
    left = 188
    top_y = 172

    def tile_color(sym: str) -> tuple[int, int, int]:
        if sym in ("🎰", "7️⃣"):
            return (169, 40, 44)
        if sym == "💎":
            return (34, 88, 143)
        if sym == "⭐":
            return (133, 103, 38)
        if sym == "🌀":
            return (48, 56, 66)
        if sym == "❓":
            return (56, 56, 56)
        return (63, 71, 50)

    grid = [top, [c1, c2, c3], bot]
    for r in range(3):
        for c in range(3):
            x0 = left + c * (col_w + gap)
            y0 = top_y + r * row_h
            x1 = x0 + col_w
            y1 = y0 + row_h - 10
            tc = tile_color(grid[r][c])
            draw.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=(tc[0], tc[1], tc[2], 255), outline=(170, 180, 188, 210), width=2)
            label = _slot_render_label(grid[r][c])
            bb = draw.textbbox((0, 0), label, font=font_sym)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
            draw.text((x0 + (col_w - tw) // 2, y0 + (row_h - th) // 2 - 8), label, fill=(246, 246, 246, 255), font=font_sym)

    # Win line and result accent.
    win_y = top_y + row_h + (row_h // 2) - 16
    draw.line((left - 14, win_y, w - left + 14, win_y), fill=(235, 235, 235, 220), width=5)
    draw.polygon([(left - 28, win_y), (left - 8, win_y - 10), (left - 8, win_y + 10)], fill=(235, 235, 235, 220))
    draw.polygon([(w - left + 28, win_y), (w - left + 8, win_y - 10), (w - left + 8, win_y + 10)], fill=(235, 235, 235, 220))

    if len(revealed) == 3:
        a, b, c = revealed
        if a == b == c:
            line_c = (242, 193, 56, 255)
            badge_text = "JACKPOT"
        elif a == b or b == c or a == c:
            line_c = (74, 214, 141, 255)
            badge_text = "WIN"
        else:
            line_c = (220, 86, 86, 255)
            badge_text = "MISS"
        draw.line((left - 14, win_y, w - left + 14, win_y), fill=line_c, width=8)
        bx0, by0 = w // 2 - 82, h - 116
        bx1, by1 = w // 2 + 82, h - 74
        draw.rounded_rectangle((bx0, by0, bx1, by1), radius=12, fill=(26, 30, 36, 255), outline=line_c, width=3)
        bb = draw.textbbox((0, 0), badge_text, font=font_small)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        draw.text((w // 2 - tw // 2, by0 + (by1 - by0 - th) // 2), badge_text, fill=(242, 242, 242, 255), font=font_small)
    elif spinning:
        draw.text((w // 2 - 66, h - 103), "SPINNING", fill=(170, 190, 220, 235), font=font_small)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="slots_machine.png")


def _slots_embed_with_image(
    display: str,
    bet: int,
    uid: int,
    result_text: str = "",
    color: int = 0x5865F2,
    spinning: bool = False,
    bonus: bool = False,
    revealed: list[str] | None = None,
) -> tuple[discord.Embed, discord.File | None]:
    embed = _slots_embed(display, bet, uid, result_text=result_text, color=color, spinning=spinning, bonus=bonus)
    img_file = _slots_render_image_file(revealed or [], spinning=spinning)
    if img_file:
        # Hide legacy text box so image is the single visual.
        embed.description = None
        embed.set_image(url="attachment://slots_machine.png")
    return embed, img_file


# ══════════════════════════════════════════════════════════════════════════════
# 🃏  BLACKJACK
# ══════════════════════════════════════════════════════════════════════════════

_RANKS  = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
_SUITS  = ["♠️","♥️","♦️","♣️"]
_VALUES = {
    "A":11,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,
    "8":8,"9":9,"10":10,"J":10,"Q":10,"K":10,
}


def _new_deck() -> list[tuple[str, str]]:
    deck = [(r, s) for r in _RANKS for s in _SUITS] * 6
    random.shuffle(deck)
    return deck


def _hand_value(hand: list[tuple[str, str]]) -> int:
    total = sum(_VALUES[r] for r, _ in hand)
    aces  = sum(1 for r, _ in hand if r == "A")
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


def _fmt_hand(hand: list[tuple[str, str]], hide_second: bool = False) -> str:
    def _card_lines(rank: str, suit: str) -> list[str]:
        rank_l = rank.ljust(2)
        rank_r = rank.rjust(2)
        return [
            "╭───────╮",
            f"│{rank_l}     │",
            f"│   {suit}   │",
            f"│     {rank_r}│",
            "╰───────╯",
        ]

    def _hidden_lines() -> list[str]:
        return [
            "╭───────╮",
            "│░░░░░░░│",
            "│░  🂠  ░│",
            "│░░░░░░░│",
            "╰───────╯",
        ]

    cards: list[list[str]] = []
    for i, (r, s) in enumerate(hand):
        if hide_second and i == 1:
            cards.append(_hidden_lines())
        else:
            cards.append(_card_lines(r, s))

    if not cards:
        return "(no cards)"

    rows: list[str] = []
    for row_idx in range(5):
        rows.append("  ".join(card[row_idx] for card in cards))

    width = max(len(r) for r in rows) + 2
    framed = [
        "╭" + "─" * width + "╮",
    ]
    for r in rows:
        framed.append("│ " + r.ljust(width - 2) + " │")
    framed.append("╰" + "─" * width + "╯")

    return "```\n" + "\n".join(framed) + "\n```"


def _bj_color(status: str) -> int:
    if any(w in status for w in ("WIN", "BLACKJACK")): return 0x2ECC71
    if any(w in status for w in ("LOSE", "BUST")):     return 0xFF4444
    if "PUSH" in status:                               return 0xFFAA00
    return 0x5865F2


def _bj_render_image_file(
    player: list[tuple[str, str]],
    dealer: list[tuple[str, str]],
    hide_dealer: bool,
) -> discord.File | None:
    if Image is None or ImageDraw is None or ImageFont is None:
        return None

    card_w, card_h = 132, 188
    gap = 22
    side = 24
    top = 20
    row_gap = 42
    label_h = 34

    visible_dealer = dealer if not hide_dealer else dealer[:1] + [dealer[1]]
    max_cards = max(len(player), len(visible_dealer), 2)
    width = side * 2 + max_cards * card_w + (max_cards - 1) * gap
    height = top * 2 + label_h + card_h + row_gap + label_h + card_h + 24

    img = Image.new("RGBA", (width, height), (7, 48, 36, 255))
    draw = ImageDraw.Draw(img)

    # Subtle felt panel layers for depth.
    draw.rounded_rectangle((3, 3, width - 3, height - 3), radius=22, fill=(10, 66, 49, 255), outline=(88, 170, 132, 235), width=3)
    draw.rounded_rectangle((12, 12, width - 12, height - 12), radius=18, fill=(8, 58, 44, 255), outline=(43, 108, 82, 220), width=2)

    # Very light felt texture lines.
    for y in range(18, height - 18, 8):
        draw.line((18, y, width - 18, y), fill=(16, 78, 58, 32), width=1)

    try:
        font_rank = ImageFont.truetype("DejaVuSans-Bold.ttf", 36)
        font_center = ImageFont.truetype("DejaVuSans-Bold.ttf", 62)
        font_label = ImageFont.truetype("DejaVuSans-Bold.ttf", 30)
    except Exception:
        font_rank = ImageFont.load_default()
        font_center = ImageFont.load_default()
        font_label = ImageFont.load_default()

    def suit_color(suit: str) -> tuple[int, int, int]:
        if "♥" in suit or "♦" in suit:
            return (210, 44, 44)
        return (28, 28, 28)

    def _draw_card_glow(x: int, y: int, glow_rgb: tuple[int, int, int]) -> None:
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for spread, alpha in ((8, 34), (5, 52), (3, 74)):
            gd.rounded_rectangle(
                (x - spread, y - spread, x + card_w + spread, y + card_h + spread),
                radius=12 + spread,
                outline=(glow_rgb[0], glow_rgb[1], glow_rgb[2], alpha),
                width=1,
            )
        img.alpha_composite(glow)

    def draw_card(x: int, y: int, rank: str, suit: str, hidden: bool = False) -> None:
        if hidden:
            _draw_card_glow(x, y, (76, 190, 142))
            draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=11, fill=(30, 120, 86, 255), outline=(110, 220, 174, 255), width=3)
            # Use a geometric card-back motif so it renders consistently across fonts.
            pad = 14
            draw.rounded_rectangle((x + pad, y + pad, x + card_w - pad, y + card_h - pad), radius=8, outline=(195, 243, 224, 235), width=2)
            for yy in range(y + pad + 6, y + card_h - pad - 5, 10):
                draw.line((x + pad + 3, yy, x + card_w - pad - 3, yy), fill=(165, 228, 202, 120), width=1)
            inner = 20
            draw.rounded_rectangle((x + inner, y + inner + 10, x + card_w - inner, y + card_h - inner - 10), radius=6, outline=(228, 246, 238, 230), width=2)
            return

        _draw_card_glow(x, y, (118, 225, 174))
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=11, fill=(248, 248, 248, 255), outline=(56, 176, 120, 255), width=3)
        c = suit_color(suit)
        suit_char = "♥" if "♥" in suit else "♦" if "♦" in suit else "♣" if "♣" in suit else "♠"
        rank_bbox = draw.textbbox((0, 0), rank, font=font_rank)
        rank_w = rank_bbox[2] - rank_bbox[0]
        rank_h = rank_bbox[3] - rank_bbox[1]
        suit_bbox = draw.textbbox((0, 0), suit_char, font=font_center)
        suit_w = suit_bbox[2] - suit_bbox[0]
        suit_h = suit_bbox[3] - suit_bbox[1]

        top_pad = 12
        side_pad = 12
        draw.text((x + side_pad, y + top_pad), rank, fill=c, font=font_rank)
        draw.text((x + (card_w - suit_w) // 2, y + (card_h - suit_h) // 2 - 4), suit_char, fill=c, font=font_center)
        draw.text((x + card_w - rank_w - side_pad, y + card_h - rank_h - top_pad), rank, fill=c, font=font_rank)

    draw.text((side, top), "Dealer", fill=(226, 245, 235), font=font_label)
    y_dealer = top + label_h
    for i, (r, s) in enumerate(dealer):
        x = side + i * (card_w + gap)
        draw_card(x, y_dealer, r, s, hidden=(hide_dealer and i == 1))

    draw.text((side, y_dealer + card_h + row_gap - 10), "You", fill=(226, 245, 235), font=font_label)
    y_player = y_dealer + card_h + row_gap
    for i, (r, s) in enumerate(player):
        x = side + i * (card_w + gap)
        draw_card(x, y_player, r, s, hidden=False)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="blackjack_table.png")


def _bj_embed_with_image(
    player: list,
    dealer: list,
    bet: int,
    uid: int,
    status: str = "",
    hide_dealer: bool = True,
    bonus: bool = False,
) -> tuple[discord.Embed, discord.File | None]:
    embed = _bj_embed(player, dealer, bet, uid, status, hide_dealer=hide_dealer, bonus=bonus)
    img_file = _bj_render_image_file(player, dealer, hide_dealer)
    if img_file:
        # Remove old text-based hand rendering to avoid showing duplicate tables.
        embed.clear_fields()
        pv = _hand_value(player)
        dv = _hand_value(dealer) if not hide_dealer else "?"
        embed.add_field(name="🤖 Dealer", value=f"Total: **{dv}**", inline=True)
        embed.add_field(name="👤 You", value=f"Total: **{pv}**", inline=True)
        embed.add_field(name="💰 Bet", value=f"`{bet:,}` PokeCoins", inline=True)
        embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
        if status:
            embed.add_field(name="📊 Status", value=status, inline=False)
        if bonus:
            embed.add_field(name="⚡ CASINO BONUS", value="🎊 **3× MULTIPLIER** applied!", inline=False)
        if hide_dealer:
            embed.set_footer(
                text="👊 Hit = draw  •  ✋ Stand = end turn  •  💰 Double = 2× bet + 1 card  •  ✂️ Split = split pairs"
            )
        embed.set_image(url="attachment://blackjack_table.png")
    return embed, img_file


def _bj_embed(
    player: list, dealer: list, bet: int, uid: int,
    status: str = "", hide_dealer: bool = True, bonus: bool = False,
) -> discord.Embed:
    pv = _hand_value(player)
    dv = _hand_value(dealer) if not hide_dealer else "?"
    color = 0xFFD700 if bonus else _bj_color(status)
    title = "🃏 🌟 BONUS ROUND! 🌟" if bonus else "🃏 Blackjack"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(
        name=f"🤖 Dealer  (Total: {dv})",
        value=_fmt_hand(dealer, hide_second=hide_dealer),
        inline=False,
    )
    embed.add_field(
        name=f"👤 You  (Total: {pv})",
        value=_fmt_hand(player),
        inline=False,
    )
    embed.add_field(name="💰 Bet",     value=f"`{bet:,}` PokeCoins",       inline=True)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    if status:
        embed.add_field(name="📊 Status", value=status, inline=False)
    if bonus:
        embed.add_field(name="⚡ CASINO BONUS", value="🎊 **3× MULTIPLIER** applied!", inline=False)
    if hide_dealer:
        embed.set_footer(
            text="👊 Hit = draw  •  ✋ Stand = end turn  •  💰 Double = 2× bet + 1 card  •  ✂️ Split = split pairs"
        )
    return embed


class BlackjackView(discord.ui.View):
    def __init__(
        self, uid: int, bet: int,
        player: list, dealer: list, deck: list,
        replay_action=None,
    ):
        super().__init__(timeout=120)
        self.uid    = uid
        self.bet    = bet
        self.player = player
        self.dealer = dealer
        self.deck   = deck
        self.replay_action = replay_action
        self._refresh_split()

    def _refresh_split(self) -> None:
        can_split = (
            len(self.player) == 2
            and self.player[0][0] == self.player[1][0]
            and _wallet(self.uid) >= self.bet
        )
        for child in self.children:
            if getattr(child, "label", None) == "Split":
                child.disabled = not can_split  # type: ignore[attr-defined]

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

    async def _resolve(
        self, interaction: discord.Interaction, player_bust: bool = False,
    ) -> None:
        self._disable_all()
        self.stop()
        uid = self.uid

        if player_bust:
            WALLETS[uid] = _wallet(uid) - self.bet
            _record_loss(uid, self.bet)
            status = f"💥 **BUST!** Over 21 — lost **{self.bet:,}** PokeCoins."
            embed, img_file = _bj_embed_with_image(
                self.player, self.dealer, self.bet, uid, status, hide_dealer=False
            )
            await interaction.response.edit_message(
                embed=embed,
                view=PlayAgainView(uid, f"blackjack bet:{self.bet}", replay_action=self.replay_action),
                attachments=[img_file] if img_file else [],
            )
            return

        # Dealer draws to soft 17
        while _hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())

        pv, dv = _hand_value(self.player), _hand_value(self.dealer)
        bonus = False

        if dv > 21 or pv > dv:
            win, bonus = _bonus_roll(uid, self.bet)
            WALLETS[uid] = _wallet(uid) + win
            _record_win(uid, win)
            status = (
                f"🏆 **WIN!** +**{win:,}** PokeCoins!"
                + (" 🌟 **3× BONUS!**" if bonus else "")
            )
        elif pv == dv:
            status = "🤝 **PUSH** — Bet returned."
        else:
            WALLETS[uid] = _wallet(uid) - self.bet
            _record_loss(uid, self.bet)
            status = f"💸 **LOSE** — −**{self.bet:,}** PokeCoins."

        embed, img_file = _bj_embed_with_image(
            self.player, self.dealer, self.bet, uid,
            status, hide_dealer=False, bonus=bonus,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=PlayAgainView(uid, f"blackjack bet:{self.bet}", replay_action=self.replay_action),
            attachments=[img_file] if img_file else [],
        )

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message(
                "❌ This isn't your game!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="👊")
    async def hit(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.player.append(self.deck.pop())
        pv = _hand_value(self.player)
        if pv > 21:
            await self._resolve(interaction, player_bust=True)
        elif pv == 21:
            await self._resolve(interaction)
        else:
            self._refresh_split()
            embed, img_file = _bj_embed_with_image(
                self.player, self.dealer, self.bet, self.uid, "🎯 Card drawn — your move!"
            )
            await interaction.response.edit_message(
                embed=embed,
                view=self,
                attachments=[img_file] if img_file else [],
            )

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.danger, emoji="✋")
    async def stand(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self._resolve(interaction)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.success, emoji="💰")
    async def double_down(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if _wallet(self.uid) < self.bet:
            await interaction.response.send_message(
                "❌ Not enough PokeCoins to double!", ephemeral=True
            )
            return
        self.bet *= 2
        self.player.append(self.deck.pop())
        pv = _hand_value(self.player)
        if pv > 21:
            await self._resolve(interaction, player_bust=True)
        else:
            await self._resolve(interaction)

    @discord.ui.button(label="Split", style=discord.ButtonStyle.secondary, emoji="✂️")
    async def split(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if _wallet(self.uid) < self.bet:
            await interaction.response.send_message(
                "❌ Not enough PokeCoins to split!", ephemeral=True
            )
            return
        # Simple split: keep first card, draw fresh second card
        self.player = [self.player[0], self.deck.pop()]
        self._refresh_split()
        embed, img_file = _bj_embed_with_image(
            self.player, self.dealer, self.bet, self.uid,
            "✂️ Split! New hand dealt — your move!",
        )
        await interaction.response.edit_message(
            embed=embed,
            view=self,
            attachments=[img_file] if img_file else [],
        )

    async def on_timeout(self) -> None:
        self._disable_all()


# ══════════════════════════════════════════════════════════════════════════════
# 🪙  COIN FLIP
# ══════════════════════════════════════════════════════════════════════════════

def _load_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    try:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        return ImageFont.truetype(name, size)
    except Exception:
        return ImageFont.load_default()


def _casino_panel_canvas(
    width: int = 980,
    height: int = 560,
    accent: tuple[int, int, int] = (87, 170, 129),
) -> tuple[object | None, object | None]:
    if Image is None or ImageDraw is None:
        return None, None
    img = Image.new("RGBA", (width, height), (8, 52, 40, 255))
    draw = ImageDraw.Draw(img)
    for y in range(0, height, 4):
        shade = 28 + ((y // 4) % 2) * 2
        draw.line((0, y, width, y), fill=(8, shade, 26, 255), width=2)
    draw.rounded_rectangle((12, 12, width - 12, height - 12), radius=34, outline=(*accent, 230), width=4)
    draw.rounded_rectangle((36, 36, width - 36, height - 36), radius=26, fill=(17, 24, 30, 255), outline=(58, 70, 84, 220), width=2)
    return img, draw


def _save_panel_file(img: object, filename: str) -> discord.File:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)


def _coinflip_render_image_file(result: str, spinning: bool = False) -> discord.File | None:
    img, draw = _casino_panel_canvas(accent=(213, 163, 54))
    if img is None or draw is None:
        return None

    font_title = _load_font(46, bold=True)
    font_big = _load_font(72, bold=True)
    font_small = _load_font(24, bold=True)

    draw.text((348, 68), "COIN FLIP", fill=(244, 211, 112, 255), font=font_title)

    coin_box = (330, 165, 650, 485)
    draw.ellipse(coin_box, fill=(52, 60, 70, 255), outline=(192, 154, 63, 255), width=8)
    draw.ellipse((346, 181, 634, 469), fill=(178, 138, 56, 255), outline=(228, 195, 111, 255), width=5)

    if spinning:
        draw.text((444, 285), "..", fill=(245, 245, 245, 255), font=font_big)
        draw.text((416, 420), "FLIPPING", fill=(197, 212, 234, 240), font=font_small)
    else:
        face = "H" if result == "heads" else "T"
        draw.text((448, 276), face, fill=(250, 247, 231, 255), font=font_big)
        draw.text((410, 420), result.upper(), fill=(244, 244, 244, 255), font=font_small)
        draw.ellipse((410, 230, 448, 268), fill=(255, 255, 255, 210))

    return _save_panel_file(img, "coinflip_table.png")


def _coinflip_embed_with_image(
    result: str,
    choice: str,
    bet: int,
    uid: int,
    spinning: bool = False,
    bonus: bool = False,
) -> tuple[discord.Embed, discord.File | None]:
    embed = _coinflip_embed(result, choice, bet, uid, spinning=spinning, bonus=bonus)
    img_file = _coinflip_render_image_file(result, spinning=spinning)
    if img_file:
        embed.description = None
        embed.set_image(url="attachment://coinflip_table.png")
    return embed, img_file

def _coinflip_embed(
    result: str, choice: str, bet: int, uid: int,
    spinning: bool = False, bonus: bool = False,
) -> discord.Embed:
    if spinning:
        return discord.Embed(
            title="🪙 Coin Flip  ·  🌀 Flipping...",
            description=(
                "```\n"
                "  ╔══════════╗\n"
                "  ║  🌀🌀🌀  ║\n"
                "  ║ SPINNING ║\n"
                "  ║  🌀🌀🌀  ║\n"
                "  ╚══════════╝\n"
                "```"
            ),
            color=0xFFAA00,
        )
    won   = result == choice
    emoji = "👑" if result == "heads" else "🦅"
    color = 0xFFD700 if bonus else (0x2ECC71 if won else 0xFF4444)
    title = f"🪙 {'🌟 BONUS! ' if bonus else ''}Coin Flip  ·  {emoji} {result.upper()}!"
    embed = discord.Embed(title=title, color=color)
    embed.description = (
        f"```\n"
        f"  ╔══════════╗\n"
        f"  ║          ║\n"
        f"  ║    {emoji}    ║\n"
        f"  ║ {result.upper():<8} ║\n"
        f"  ╚══════════╝\n"
        f"```"
    )
    embed.add_field(name="Your Pick", value=f"`{choice}`",  inline=True)
    embed.add_field(name="Result",    value=f"`{result}`",  inline=True)
    if won:
        win_amt = bet * (3 if bonus else 1)
        embed.add_field(
            name="📊 Outcome",
            value=f"🎉 **WON** `{win_amt:,}` PokeCoins!" + (" 🌟 **3× BONUS!**" if bonus else ""),
            inline=False,
        )
    else:
        embed.add_field(name="📊 Outcome", value=f"💸 **LOST** `{bet:,}` PokeCoins.", inline=False)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    embed.set_footer(text="🪙 /coinflip  |  🎰 /slots  |  🎲 /dice  |  🃏 /blackjack")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# 🎡  ROULETTE
# ══════════════════════════════════════════════════════════════════════════════

_RED_NUMS   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
_BLACK_NUMS = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}
_WHEEL_ORDER = [
    0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,10,5,
    24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26,
]


def _r_color(n: int) -> tuple[str, str]:
    if n == 0:           return "🟢", "green"
    if n in _RED_NUMS:   return "🔴", "red"
    return "⚫", "black"


def _wheel_slice(landed: int) -> str:
    """Show 5 numbers around the landing spot on the wheel."""
    idx  = _WHEEL_ORDER.index(landed)
    prev = [_WHEEL_ORDER[(idx - i - 1) % len(_WHEEL_ORDER)] for i in range(2)][::-1]
    nxt  = [_WHEEL_ORDER[(idx + i + 1) % len(_WHEEL_ORDER)] for i in range(2)]

    def fmt(n: int) -> str:
        e, _ = _r_color(n)
        return f"{e}{n:02d}"

    centre = f"[{fmt(landed)}]"
    line   = "  ".join([fmt(n) for n in prev] + [centre] + [fmt(n) for n in nxt])
    return f"```\n🎡  {line}  🎡\n         ▲ BALL\n```"


def _roulette_embed(
    landed: int | None, choice: str, bet: int, uid: int,
    spinning: bool = False, bonus: bool = False, countdown: int | None = None,
) -> discord.Embed:
    if spinning:
        title = "🎡 Roulette  ·  🌀 Spinning..."
        if countdown is not None:
            title += f"  `{countdown}s`"
        embed = discord.Embed(title=title, color=0x5865F2)
        embed.add_field(name="🎲 Your Bet", value=f"`{choice}` → `{bet:,}` coins", inline=True)
        embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
        if countdown is not None:
            if countdown > 5:
                embed.add_field(name="⏱️ Betting Window", value=f"Ball spinning... bets lock in **{countdown}s**", inline=False)
            else:
                embed.add_field(name="⏱️ Betting Window", value=f"🚫 No more bets... resolving in **{countdown}s**", inline=False)
        return embed
    assert landed is not None
    emoji, color_name = _r_color(landed)
    is_num = choice.lstrip("-").isdigit()
    if is_num:        won = landed == int(choice); mult = 35
    elif choice == "green":  won = landed == 0;         mult = 17
    elif choice == "red":    won = landed in _RED_NUMS;  mult = 1
    else:                    won = landed in _BLACK_NUMS; mult = 1
    if bonus and won:
        mult *= 3
    color = 0xFFD700 if bonus else (0x2ECC71 if won else 0xFF4444)
    title = f"🎡 {'🌟 BONUS! ' if bonus else ''}Roulette  ·  {emoji} **{landed}** ({color_name})"
    embed = discord.Embed(title=title, color=color)
    embed.description = _wheel_slice(landed)
    payout  = bet * mult if won else 0
    outcome = (
        f"🎉 **WIN!** +**{payout:,}** PokeCoins (×{mult})"
        + (" 🌟 **3× BONUS!**" if bonus and won else "")
        if won else
        f"💸 **LOSE** −**{bet:,}** PokeCoins"
    )
    embed.add_field(name="🎯 Ball Landed", value=f"{emoji} `{landed}` ({color_name})", inline=True)
    embed.add_field(name="🎲 Your Bet",    value=f"`{choice}` → `{bet:,}` coins",      inline=True)
    embed.add_field(name="📊 Outcome",     value=outcome,                               inline=False)
    embed.add_field(name="💼 Balance",     value=f"`{_wallet(uid):,}` PokeCoins",       inline=True)
    embed.set_footer(text="Red/Black 1:1  •  Green 17:1  •  Number 35:1  |  /roulette to play again")
    return embed


def _roulette_render_image_file(
    landed: int | None,
    spinning: bool = False,
    spin_phase: float = 0.0,
) -> discord.File | None:
    if Image is None or ImageDraw is None or ImageFont is None:
        return None

    width, height = 1100, 620
    img = Image.new("RGBA", (width, height), (14, 23, 19, 255))
    draw = ImageDraw.Draw(img)

    # Background glow.
    draw.ellipse((-120, -80, width + 120, height + 180), fill=(31, 54, 42, 120))

    # Table body.
    table = (28, 44, width - 28, height - 44)
    draw.rounded_rectangle(table, radius=70, fill=(44, 90, 40, 255), outline=(228, 205, 150, 255), width=8)
    draw.rounded_rectangle((44, 60, width - 44, height - 60), radius=60, fill=(37, 113, 46, 255), outline=(248, 232, 192, 170), width=2)

    # Left wheel panel and right betting panel.
    left_panel = (52, 92, 480, 528)
    right_panel = (500, 92, 1048, 528)
    draw.rounded_rectangle(left_panel, radius=26, fill=(74, 45, 19, 255), outline=(204, 170, 116, 255), width=3)
    draw.rounded_rectangle(right_panel, radius=26, fill=(28, 112, 45, 255), outline=(200, 232, 204, 130), width=3)

    cx, cy = 266, 310
    outer_r = 188
    mid_r = 162
    inner_r = 130
    hub_r = 52
    seg_deg = 360.0 / len(_WHEEL_ORDER)

    # Wheel wood/metal rings.
    draw.ellipse((cx - outer_r - 18, cy - outer_r - 18, cx + outer_r + 18, cy + outer_r + 18), fill=(209, 168, 102, 255))
    draw.ellipse((cx - outer_r - 8, cy - outer_r - 8, cx + outer_r + 8, cy + outer_r + 8), fill=(123, 76, 34, 255))
    draw.ellipse((cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r), fill=(40, 40, 40, 255))

    # Segment ring.
    for i, n in enumerate(_WHEEL_ORDER):
        a0 = -90 + i * seg_deg
        a1 = a0 + seg_deg
        if n == 0:
            col = (22, 152, 76, 255)
        elif n in _RED_NUMS:
            col = (200, 28, 28, 255)
        else:
            col = (32, 32, 36, 255)
        draw.pieslice((cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r), a0, a1, fill=col)
        draw.pieslice((cx - mid_r, cy - mid_r, cx + mid_r, cy + mid_r), a0, a1, fill=(18, 18, 19, 255))

    try:
        font_num = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
        font_table_num = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        font_table_small = ImageFont.truetype("DejaVuSans-Bold.ttf", 17)
    except Exception:
        font_num = ImageFont.load_default()
        font_table_num = ImageFont.load_default()
        font_table_small = ImageFont.load_default()

    # Wheel labels.
    label_r = (outer_r + mid_r) // 2
    for i, n in enumerate(_WHEEL_ORDER):
        ang = math.radians(-90 + (i + 0.5) * seg_deg)
        tx = int(cx + math.cos(ang) * label_r)
        ty = int(cy + math.sin(ang) * label_r)
        txt = str(n)
        bb = draw.textbbox((0, 0), txt, font=font_num)
        draw.text((tx - (bb[2] - bb[0]) // 2, ty - (bb[3] - bb[1]) // 2), txt, fill=(240, 240, 240, 255), font=font_num)

    # Inner bowl and hub.
    draw.ellipse((cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r), fill=(122, 76, 34, 255), outline=(228, 188, 128, 255), width=3)
    draw.ellipse((cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r), fill=(168, 120, 62, 255), outline=(238, 210, 155, 255), width=3)

    # Spokes.
    for deg in (20, 140, 260):
        a = math.radians(deg)
        x2 = int(cx + math.cos(a) * (hub_r + 28))
        y2 = int(cy + math.sin(a) * (hub_r + 28))
        draw.line((cx, cy, x2, y2), fill=(236, 215, 173, 230), width=3)

    # Ball movement.
    ball_track_r = outer_r - 10
    if spinning:
        # Ease-out motion so the ball starts fast and slows near the end.
        t = max(0.0, min(1.0, spin_phase))
        eased = 1.0 - ((1.0 - t) ** 2.6)
        turns = 7.0
        ball_ang = math.radians(-90 + ((eased * turns * 360.0) % 360.0))
    else:
        target = landed if landed is not None else random.choice(_WHEEL_ORDER)
        target_idx = _WHEEL_ORDER.index(target)
        ball_ang = math.radians(-90 + (target_idx + 0.5) * seg_deg)

    bx = int(cx + math.cos(ball_ang) * ball_track_r)
    by = int(cy + math.sin(ball_ang) * ball_track_r)
    br = 10
    draw.ellipse((bx - br + 2, by - br + 2, bx + br + 2, by + br + 2), fill=(0, 0, 0, 130))
    draw.ellipse((bx - br, by - br, bx + br, by + br), fill=(220, 220, 220, 255), outline=(250, 250, 250, 220), width=1)
    draw.ellipse((bx - 3, by - 4, bx + 1, by), fill=(255, 255, 255, 220))

    # Betting grid (right side) like a roulette felt table.
    gx0, gy0 = 538, 154
    cell_w, cell_h = 40, 58

    # Zero column.
    draw.rectangle((gx0 - 34, gy0, gx0, gy0 + cell_h * 3), fill=(12, 130, 68, 255), outline=(220, 238, 220, 230), width=2)
    zbb = draw.textbbox((0, 0), "0", font=font_table_num)
    draw.text((gx0 - 17 - (zbb[2] - zbb[0]) // 2, gy0 + (cell_h * 3 - (zbb[3] - zbb[1])) // 2), "0", fill=(245, 245, 245, 255), font=font_table_num)

    for col in range(12):
        nums = [3 + col * 3, 2 + col * 3, 1 + col * 3]
        for row, n in enumerate(nums):
            x0 = gx0 + col * cell_w
            y0 = gy0 + row * cell_h
            x1 = x0 + cell_w
            y1 = y0 + cell_h
            if n in _RED_NUMS:
                fill = (198, 28, 28, 255)
            else:
                fill = (28, 33, 37, 255)
            draw.rectangle((x0, y0, x1, y1), fill=fill, outline=(220, 238, 220, 210), width=2)
            t = str(n)
            bb = draw.textbbox((0, 0), t, font=font_table_num)
            draw.text((x0 + (cell_w - (bb[2] - bb[0])) // 2, y0 + (cell_h - (bb[3] - bb[1])) // 2), t, fill=(244, 244, 244, 255), font=font_table_num)

    # Dozens row.
    y_dozen = gy0 + cell_h * 3 + 10
    labels = ["1st 12", "2nd 12", "3rd 12"]
    for i, lbl in enumerate(labels):
        x0 = gx0 + i * 160
        x1 = x0 + 156
        draw.rectangle((x0, y_dozen, x1, y_dozen + 42), fill=(44, 98, 45, 255), outline=(220, 238, 220, 210), width=2)
        bb = draw.textbbox((0, 0), lbl, font=font_table_small)
        draw.text((x0 + (156 - (bb[2] - bb[0])) // 2, y_dozen + (42 - (bb[3] - bb[1])) // 2), lbl, fill=(234, 244, 234, 255), font=font_table_small)

    # Even chance row.
    y_even = y_dozen + 50
    labels2 = ["1 to 18", "EVEN", "RED", "BLACK", "ODD", "19 to 36"]
    widths = [80, 70, 70, 80, 70, 90]
    x = gx0
    for i, lbl in enumerate(labels2):
        w = widths[i]
        fill = (38, 88, 41, 255)
        if lbl == "RED":
            fill = (198, 28, 28, 255)
        elif lbl == "BLACK":
            fill = (24, 26, 30, 255)
        draw.rectangle((x, y_even, x + w, y_even + 42), fill=fill, outline=(220, 238, 220, 200), width=2)
        bb = draw.textbbox((0, 0), lbl, font=font_table_small)
        draw.text((x + (w - (bb[2] - bb[0])) // 2, y_even + (42 - (bb[3] - bb[1])) // 2), lbl, fill=(238, 244, 238, 255), font=font_table_small)
        x += w

    # Chips near top-right for style.
    for cx2, cy2, col in ((720, 112, (220, 42, 42, 255)), (742, 142, (220, 42, 42, 255)), (772, 118, (24, 24, 28, 255))):
        draw.ellipse((cx2 - 12, cy2 - 12, cx2 + 12, cy2 + 12), fill=col, outline=(250, 250, 250, 180), width=2)
        draw.ellipse((cx2 - 4, cy2 - 4, cx2 + 4, cy2 + 4), fill=(245, 245, 245, 220))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="roulette_wheel.png")


def _roulette_embed_with_image(
    landed: int | None,
    choice: str,
    bet: int,
    uid: int,
    spinning: bool = False,
    bonus: bool = False,
    spin_phase: float = 0.0,
    countdown: int | None = None,
) -> tuple[discord.Embed, discord.File | None]:
    embed = _roulette_embed(landed, choice, bet, uid, spinning=spinning, bonus=bonus, countdown=countdown)
    img_file = _roulette_render_image_file(landed, spinning=spinning, spin_phase=spin_phase)
    if img_file:
        # Remove old text wheel so only the new rendered wheel is shown.
        embed.description = None
        embed.set_image(url="attachment://roulette_wheel.png")
    return embed, img_file


# ══════════════════════════════════════════════════════════════════════════════
# 🎲  DICE DUEL
# ══════════════════════════════════════════════════════════════════════════════

_DICE_FACES = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]


def _dice_embed(
    p_roll: int | None, b_roll: int | None,
    bet: int, uid: int,
    spinning: bool = False,
) -> discord.Embed:
    if spinning:
        return discord.Embed(
            title="🎲 Dice Duel  ·  🌀 Rolling...",
            description=(
                "```\n"
                "  ╔═══════╗  ╔═══════╗\n"
                "  ║   ?   ║  ║   ?   ║\n"
                "  ╚═══════╝  ╚═══════╝\n"
                "    YOU         BOT\n"
                "```"
            ),
            color=0xFF6B35,
        )
    assert p_roll is not None and b_roll is not None
    pf = _DICE_FACES[p_roll - 1]
    bf = _DICE_FACES[b_roll - 1]
    bonus = False
    if p_roll > b_roll:
        win, bonus = _bonus_roll(uid, bet)
        WALLETS[uid] = _wallet(uid) + win
        _record_win(uid, win)
        color  = 0xFFD700 if bonus else 0x2ECC71
        result = f"🏆 **YOU WIN!** +**{win:,}** PokeCoins!" + (" 🌟 **3× BONUS!**" if bonus else "")
    elif p_roll == b_roll:
        color  = 0xFFAA00
        result = "🤝 **TIE** — Bet returned."
    else:
        WALLETS[uid] = _wallet(uid) - bet
        _record_loss(uid, bet)
        color  = 0xFF4444
        result = f"💸 **YOU LOSE** −**{bet:,}** PokeCoins."
    embed = discord.Embed(
        title=f"🎲 {'🌟 BONUS! ' if bonus else ''}Dice Duel", color=color
    )
    embed.description = (
        f"```\n"
        f"  ╔═══════╗  ╔═══════╗\n"
        f"  ║  {pf}   ║  ║  {bf}   ║\n"
        f"  ║   {p_roll}   ║  ║   {b_roll}   ║\n"
        f"  ╚═══════╝  ╚═══════╝\n"
        f"    YOU         BOT\n"
        f"```"
    )
    embed.add_field(name="📊 Result",  value=result,                          inline=False)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    if bonus:
        embed.add_field(name="⚡ CASINO BONUS", value="🎊 3× MULTIPLIER!", inline=True)
    embed.set_footer(text="Roll higher than the bot to win!  |  /dice to play again")
    return embed


def _dice_render_image_file(p_roll: int | None, b_roll: int | None, spinning: bool = False) -> discord.File | None:
    img, draw = _casino_panel_canvas(accent=(111, 187, 153))
    if img is None or draw is None:
        return None

    font_title = _load_font(42, bold=True)
    font_die = _load_font(70, bold=True)
    font_num = _load_font(34, bold=True)
    font_lbl = _load_font(24, bold=True)

    draw.text((388, 66), "DICE DUEL", fill=(194, 234, 214, 255), font=font_title)

    boxes = [(220, 180, 450, 430), (530, 180, 760, 430)]
    labels = ["YOU", "BOT"]
    vals = [p_roll, b_roll]
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        draw.rounded_rectangle((x0, y0, x1, y1), radius=24, fill=(30, 37, 44, 255), outline=(133, 147, 166, 255), width=3)
        if spinning:
            face = "?"
            num = "-"
        else:
            assert vals[i] is not None
            face = _DICE_FACES[vals[i] - 1]
            num = str(vals[i])
        draw.text((x0 + 86, y0 + 58), face, fill=(247, 247, 247, 255), font=font_die)
        draw.text((x0 + 103, y0 + 160), num, fill=(235, 235, 235, 255), font=font_num)
        draw.text((x0 + 88, y1 + 18), labels[i], fill=(170, 195, 210, 240), font=font_lbl)

    return _save_panel_file(img, "dice_table.png")


def _dice_embed_with_image(
    p_roll: int | None,
    b_roll: int | None,
    bet: int,
    uid: int,
    spinning: bool = False,
) -> tuple[discord.Embed, discord.File | None]:
    embed = _dice_embed(p_roll, b_roll, bet, uid, spinning=spinning)
    img_file = _dice_render_image_file(p_roll, b_roll, spinning=spinning)
    if img_file:
        embed.description = None
        embed.set_image(url="attachment://dice_table.png")
    return embed, img_file


# ══════════════════════════════════════════════════════════════════════════════
# 🃏  HIGH-LOW  (new)
# ══════════════════════════════════════════════════════════════════════════════

_HL_VALUES = {
    "A":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,
    "8":8,"9":9,"10":10,"J":11,"Q":12,"K":13,
}


def _hl_rank_val(r: str) -> int:
    return _HL_VALUES[r]


def _hl_embed(
    current: tuple[str, str],
    next_card: tuple[str, str] | None,
    guess: str, bet: int, uid: int,
    won: bool = False, result: str = "",
    spinning: bool = False, bonus: bool = False,
) -> discord.Embed:
    r, s = current
    if spinning:
        return discord.Embed(
            title="🃏 High-Low  ·  🌀 Drawing...",
            description=(
                f"**Current Card:** `{r}{s}`\n\n"
                "Drawing the next card..."
            ),
            color=0x9B59B6,
        )
    nr, ns = next_card  # type: ignore[misc]
    color = 0xFFD700 if bonus else (0x2ECC71 if won else 0xFF4444)
    embed = discord.Embed(
        title=f"🃏 {'🌟 BONUS! ' if bonus else ''}High-Low",
        color=color,
    )
    embed.description = (
        f"```\n"
        f"  ╔════╗     ╔════╗\n"
        f"  ║{r:<4}║  →  ║{nr:<4}║\n"
        f"  ║    ║     ║    ║\n"
        f"  ║  {s} ║     ║  {ns} ║\n"
        f"  ╚════╝     ╚════╝\n"
        f"  CURRENT    NEXT\n"
        f"```\n"
        f"Your guess: **{guess}**"
    )
    embed.add_field(name="📊 Result",  value=result,                          inline=False)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    if bonus:
        embed.add_field(name="⚡ CASINO BONUS", value="🎊 3× MULTIPLIER!", inline=True)
    embed.set_footer(text="Aces are LOW (1)  |  Correct guess = 1.9× payout  |  /highlow to play again")
    return embed


def _hl_render_image_file(
    current: tuple[str, str],
    next_card: tuple[str, str] | None,
    spinning: bool = False,
) -> discord.File | None:
    img, draw = _casino_panel_canvas(accent=(166, 126, 209))
    if img is None or draw is None:
        return None

    font_title = _load_font(42, bold=True)
    font_rank = _load_font(54, bold=True)
    font_suit = _load_font(40, bold=False)
    font_lbl = _load_font(22, bold=True)

    draw.text((385, 64), "HIGH LOW", fill=(222, 194, 244, 255), font=font_title)

    cards = [current, ("?", "?") if spinning or next_card is None else next_card]
    x_positions = [260, 560]
    labels = ["CURRENT", "NEXT"]

    for i, (rank, suit) in enumerate(cards):
        x0, y0 = x_positions[i], 170
        x1, y1 = x0 + 170, y0 + 245
        draw.rounded_rectangle((x0, y0, x1, y1), radius=16, fill=(248, 248, 248, 255), outline=(82, 190, 142, 220), width=3)
        sc = (210, 44, 44) if suit in {"♥️", "♦️"} else (26, 26, 28)
        suit_ch = "?" if suit == "?" else ("♥" if "♥" in suit else "♦" if "♦" in suit else "♣" if "♣" in suit else "♠")
        draw.text((x0 + 22, y0 + 18), rank, fill=sc, font=font_rank)
        draw.text((x0 + 64, y0 + 120), suit_ch, fill=sc, font=font_suit)
        draw.text((x0 + 38, y1 + 16), labels[i], fill=(178, 198, 213, 240), font=font_lbl)

    draw.text((480, 260), "->", fill=(235, 235, 235, 255), font=font_rank)
    return _save_panel_file(img, "highlow_table.png")


def _hl_embed_with_image(
    current: tuple[str, str],
    next_card: tuple[str, str] | None,
    guess: str,
    bet: int,
    uid: int,
    won: bool = False,
    result: str = "",
    spinning: bool = False,
    bonus: bool = False,
) -> tuple[discord.Embed, discord.File | None]:
    embed = _hl_embed(current, next_card, guess, bet, uid, won=won, result=result, spinning=spinning, bonus=bonus)
    img_file = _hl_render_image_file(current, next_card, spinning=spinning)
    if img_file:
        if spinning:
            embed.description = f"Drawing the next card...\n\nYour guess: **{guess}**"
        else:
            embed.description = f"Your guess: **{guess}**"
        embed.set_image(url="attachment://highlow_table.png")
    return embed, img_file


def _plinko_render_image_file(col: int | None = None, spinning: bool = False) -> discord.File | None:
    img, draw = _casino_panel_canvas(accent=(86, 166, 212))
    if img is None or draw is None:
        return None

    font_title = _load_font(42, bold=True)
    font_lbl = _load_font(20, bold=True)
    draw.text((420, 66), "PLINKO", fill=(177, 219, 246, 255), font=font_title)

    start_x, start_y = 205, 140
    step_x, step_y = 64, 42
    for r in range(8):
        off = 0 if r % 2 == 0 else step_x // 2
        count = 9 if r % 2 == 0 else 8
        for c in range(count):
            x = start_x + off + c * step_x
            y = start_y + r * step_y
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(214, 214, 214, 255))

    buckets_y = 466
    for i, lbl in enumerate(_PLINKO_LBLS):
        x0 = 172 + i * 72
        x1 = x0 + 66
        fill = (38, 47, 57, 255)
        if col is not None and i == col and not spinning:
            fill = (68, 136, 82, 255)
        draw.rounded_rectangle((x0, buckets_y, x1, 518), radius=8, fill=fill, outline=(126, 138, 150, 220), width=2)
        draw.text((x0 + 12, buckets_y + 14), lbl.strip(), fill=(237, 237, 237, 255), font=font_lbl)

    if spinning:
        bx, by = 493, 110
    else:
        landing = 205 + (col or 4) * 72
        bx, by = landing + 30, 445
    draw.ellipse((bx - 12, by - 12, bx + 12, by + 12), fill=(218, 51, 57, 255), outline=(248, 210, 210, 230), width=2)
    return _save_panel_file(img, "plinko_board.png")


def _heist_render_image_file(title: str, lines: list[str], accent: tuple[int, int, int]) -> discord.File | None:
    img, draw = _casino_panel_canvas(accent=accent)
    if img is None or draw is None:
        return None
    font_title = _load_font(38, bold=True)
    font_line = _load_font(24, bold=False)
    draw.text((74, 62), title.upper(), fill=(236, 236, 236, 255), font=font_title)

    y = 148
    for line in lines[:9]:
        clean = line.replace("**", "")
        draw.text((84, y), clean[:72], fill=(216, 226, 236, 250), font=font_line)
        y += 42
    return _save_panel_file(img, "heist_panel.png")


def _heist_lobby_embed(
    host: discord.Member,
    bet: int,
    target_key: str,
    participants: list[discord.Member],
    *,
    closed: bool = False,
) -> tuple[discord.Embed, discord.File | None]:
    target = _HEIST_TARGETS[target_key]
    payout_each = int(bet * target["mult"])
    crew_total = payout_each * len(participants)
    roster_lines = [f"• {member.display_name}" for member in participants[:HEIST_MAX_PLAYERS]]
    embed = discord.Embed(
        title="🦹 Heist Lobby",
        description=(
            f"**Captain:** {host.mention}\n"
            f"**Target:** {target['name']}\n"
            f"**Bet per player:** `{bet:,}` PokeCoins\n"
            f"**Potential payout each:** `{payout_each:,}` PokeCoins\n"
            f"**Potential crew haul:** `{crew_total:,}` PokeCoins"
        ),
        color=target["color"],
    )
    embed.add_field(name=f"👥 Crew ({len(participants)}/{HEIST_MAX_PLAYERS})", value="\n".join(roster_lines), inline=False)
    if closed:
        embed.set_footer(text="Lobby locked. Executing the heist...")
    else:
        embed.add_field(name="How it works", value="Press Join Heist to risk the same bet and ride the same outcome.", inline=False)
        embed.set_footer(text=f"Crew entry closes in {HEIST_JOIN_SECONDS} seconds.")

    panel_lines = [
        f"Target: {target['name']}",
        f"Bet each: {bet:,}",
        f"Payout each: {payout_each:,}",
        f"Crew haul: {crew_total:,}",
    ]
    panel_lines.extend(member.display_name for member in participants[:5])
    return embed, _heist_render_image_file("Heist Lobby", panel_lines, (103, 152, 214))


async def _run_heist_sequence(
    message: discord.Message,
    participants: list[discord.Member],
    bet: int,
    target_key: str,
) -> None:
    target = _HEIST_TARGETS[target_key]
    valid_participants: list[discord.Member] = []
    dropped_names: list[str] = []

    for member in participants:
        err = _check_bet(member.id, bet)
        if err is None:
            valid_participants.append(member)
        else:
            dropped_names.append(member.display_name)

    if not valid_participants:
        cancel_embed = discord.Embed(
            title="🦹 Heist Cancelled",
            description="Nobody in the crew still had enough PokeCoins when the lobby closed.",
            color=0xE74C3C,
        )
        await message.edit(embed=cancel_embed, attachments=[], view=None)
        return

    extra_players = max(0, len(valid_participants) - 1)
    player_bonus = min(extra_players * HEIST_PLAYER_BONUS, HEIST_PLAYER_BONUS_CAP)

    plan_lines = [
        f"Target: {target['name']}",
        f"Crew size: {len(valid_participants)}",
        f"Bet each: {bet:,}",
        f"Base success: {int(target['success_base'] * 100)}%",
        f"Crew synergy: +{int(player_bonus * 100)}%",
        "Studying the blueprints...",
        "Sourcing equipment...",
        "Recruiting the crew...",
    ]
    if dropped_names:
        plan_lines.append(f"Dropped: {', '.join(dropped_names[:2])}")

    plan_embed = discord.Embed(
        title="🦹 Heist Planning Room",
        description=(
            f"**Target:** {target['name']}\n"
            f"**Crew size:** `{len(valid_participants)}`\n"
            f"**Bet per player:** `{bet:,}` PokeCoins\n"
            f"**Potential payout each:** `{int(bet * target['mult']):,}` PokeCoins\n"
            f"**Base success chance:** `{int(target['success_base'] * 100)}%`\n"
            f"**Crew synergy bonus:** `+{int(player_bonus * 100)}%`"
        ),
        color=target["color"],
    )
    if dropped_names:
        plan_embed.add_field(name="Late Drops", value=", ".join(dropped_names), inline=False)
    plan_embed.add_field(
        name="Crew",
        value="\n".join(member.mention for member in valid_participants),
        inline=False,
    )
    plan_embed.set_footer(text="Heist begins in 2 seconds...")
    plan_img = _heist_render_image_file("Heist Planning Room", plan_lines, (103, 152, 214))
    if plan_img:
        plan_embed.set_image(url="attachment://heist_panel.png")
        await message.edit(embed=plan_embed, attachments=[plan_img], view=None)
    else:
        await message.edit(embed=plan_embed, attachments=[], view=None)
    await asyncio.sleep(2)

    crew_lines = [f"👥 Crew synergy bonus applied (+{int(player_bonus * 100)}%)"]
    success_mod = player_bonus
    for member in valid_participants:
        crew_lines.append(f"✅ {member.display_name} is locked in")
    for role, bonus_val in _HEIST_CREW_ROLES:
        if random.random() < 0.65:
            crew_lines.append(f"✅ {role} joined  (+{int(bonus_val * 100)}%)")
            success_mod += bonus_val
        else:
            crew_lines.append(f"❌ {role} bailed last minute")
    final_chance = min(target["success_base"] + success_mod, HEIST_SUCCESS_CAP)

    crew_embed = discord.Embed(
        title="🦹 Crew Assembled",
        description="\n".join(crew_lines[:12]),
        color=target["color"],
    )
    crew_embed.add_field(name="📊 Final Success Chance", value=f"`{int(final_chance * 100)}%`", inline=True)
    crew_embed.add_field(name="👥 Crew Size", value=f"`{len(valid_participants)}`", inline=True)
    crew_embed.add_field(name="💰 Bet Each", value=f"`{bet:,}` PokeCoins", inline=True)
    crew_embed.set_footer(text="Executing the heist...")
    crew_img = _heist_render_image_file("Crew Assembled", crew_lines, (120, 178, 137))
    if crew_img:
        crew_embed.set_image(url="attachment://heist_panel.png")
        await message.edit(embed=crew_embed, attachments=[crew_img])
    else:
        await message.edit(embed=crew_embed, attachments=[])
    await asyncio.sleep(2.5)

    stage_lines: list[str] = []
    heist_failed = False
    for action, ok_txt, fail_txt in _HEIST_STAGES:
        if random.random() < final_chance:
            stage_lines.append(f"✅ **{action}** — {ok_txt}")
        else:
            stage_lines.append(f"❌ **{action}** — {fail_txt}!")
            heist_failed = True
            break

    succeeded = (random.random() < final_chance) and not heist_failed
    bonus = False
    payout_each = int(bet * target["mult"])
    total_change = 0
    if succeeded:
        bonus = random.random() < BONUS_CHANCE
        if bonus:
            payout_each *= 3
        for member in valid_participants:
            WALLETS[member.id] = _wallet(member.id) + payout_each
            _record_win(member.id, payout_each)
        total_change = payout_each * len(valid_participants)
        color = 0xFFD700 if bonus else 0x2ECC71
        outcome = (
            f"💰 **HEIST SUCCESSFUL!** The crew escaped clean!\n\n"
            f"**+{payout_each:,} PokeCoins each**"
            + (" 🌟 **3× BONUS!**" if bonus else "")
        )
        title = f"🦹 {target['name']} — SUCCESS!"
    else:
        if random.random() < 0.30:
            recovered = int(bet * 0.4)
            loss_each = bet - recovered
            for member in valid_participants:
                WALLETS[member.id] = _wallet(member.id) - loss_each
                _record_loss(member.id, loss_each)
            total_change = -(loss_each * len(valid_participants))
            color = 0xFFAA00
            outcome = (
                f"⚠️ **PARTIAL ESCAPE!** Crew scattered — some loot was recovered.\n\n"
                f"**Lost {loss_each:,} PokeCoins each** (recovered `{recovered:,}` each)"
            )
            title = f"🦹 {target['name']} — PARTIAL"
        else:
            for member in valid_participants:
                WALLETS[member.id] = _wallet(member.id) - bet
                _record_loss(member.id, bet)
            total_change = -(bet * len(valid_participants))
            color = 0xFF4444
            outcome = (
                f"🚨 **BUSTED!** Cops caught the crew and seized everything.\n\n"
                f"**−{bet:,} PokeCoins each** confiscated."
            )
            title = f"🦹 {target['name']} — BUSTED!"

    result_embed = discord.Embed(title=title, color=color)
    result_embed.add_field(name="📋 Heist Log", value="\n".join(stage_lines) or "Couldn't even get inside.", inline=False)
    result_embed.add_field(name="📊 Outcome", value=outcome, inline=False)
    result_embed.add_field(name="👥 Crew", value=" ".join(member.mention for member in valid_participants), inline=False)
    result_embed.add_field(name="💰 Bet Each", value=f"`{bet:,}` PokeCoins", inline=True)
    result_embed.add_field(name="🏴 Crew Net", value=f"`{total_change:+,}` PokeCoins", inline=True)
    if bonus:
        result_embed.add_field(name="⚡ CASINO BONUS", value="🎊 **3× MULTIPLIER** applied to the whole crew payout!", inline=False)
    result_embed.set_footer(text="Plan your next heist with /heist!  |  /daily for free coins")

    result_lines = stage_lines if stage_lines else ["Could not get inside."]
    result_lines.append(f"Crew: {len(valid_participants)}")
    result_lines.append(outcome.replace("\n", " "))
    heist_accent = (97, 185, 125) if "SUCCESS" in title else ((196, 154, 78) if "PARTIAL" in title else (198, 79, 79))
    result_img = _heist_render_image_file(title, result_lines, heist_accent)
    if result_img:
        result_embed.set_image(url="attachment://heist_panel.png")
        await message.edit(embed=result_embed, attachments=[result_img])
    else:
        await message.edit(embed=result_embed, attachments=[])


class HeistLobbyView(discord.ui.View):
    def __init__(self, host: discord.Member, bet: int, target_key: str):
        super().__init__(timeout=HEIST_JOIN_SECONDS)
        self.host = host
        self.bet = bet
        self.target_key = target_key
        self.participants: dict[int, discord.Member] = {host.id: host}
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._resolved = False

    async def _refresh_message(self, *, closed: bool = False) -> None:
        if self.message is None:
            return
        embed, img = _heist_lobby_embed(self.host, self.bet, self.target_key, list(self.participants.values()), closed=closed)
        if img:
            embed.set_image(url="attachment://heist_panel.png")
            await self.message.edit(embed=embed, attachments=[img], view=None if closed else self)
        else:
            await self.message.edit(embed=embed, attachments=[], view=None if closed else self)

    async def _launch(self) -> None:
        async with self._lock:
            if self._resolved:
                return
            self._resolved = True
            self.stop()
            await self._refresh_message(closed=True)
        try:
            if self.message is not None:
                await _run_heist_sequence(self.message, list(self.participants.values()), self.bet, self.target_key)
        finally:
            if self.message is not None and self.message.channel:
                _HEIST_BUSY_CHANNELS.discard(self.message.channel.id)

    async def on_timeout(self) -> None:
        await self._launch()

    @discord.ui.button(label="Join Heist", style=discord.ButtonStyle.success, emoji="🦹")
    async def join_heist(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            if self._resolved:
                await interaction.response.send_message("❌ This heist lobby is already closed.", ephemeral=True)
                return
            if interaction.user.id in self.participants:
                await interaction.response.send_message("✅ You're already in this crew.", ephemeral=True)
                return
            if len(self.participants) >= HEIST_MAX_PLAYERS:
                await interaction.response.send_message("❌ This crew is already full.", ephemeral=True)
                return
            err = _check_bet(interaction.user.id, self.bet)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            self.participants[interaction.user.id] = interaction.user
            await interaction.response.send_message(
                f"✅ You joined the heist crew for **{self.bet:,}** PokeCoins.",
                ephemeral=True,
            )
            await self._refresh_message()

    @discord.ui.button(label="Start Now", style=discord.ButtonStyle.primary, emoji="🚀")
    async def start_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("❌ Only the heist captain can start early.", ephemeral=True)
            return
        await interaction.response.send_message("🚀 Launching the heist now...", ephemeral=True)
        await self._launch()


# ══════════════════════════════════════════════════════════════════════════════
# 🎯  PLINKO  (new)
# ══════════════════════════════════════════════════════════════════════════════

_PLINKO_MULTS = [0.2, 0.5, 1.0, 1.5, 3.0, 1.5, 1.0, 0.5, 0.2]   # 9 buckets
_PLINKO_LBLS  = ["0.2×","0.5×"," 1× ","1.5×"," 3× ","1.5×"," 1× ","0.5×","0.2×"]


def _plinko_drop() -> tuple[int, float]:
    pos = 4   # start centre
    for _ in range(8):
        pos += random.choice([-1, 1])
        pos  = max(0, min(8, pos))
    return pos, _PLINKO_MULTS[pos]


def _plinko_board(col: int) -> str:
    rows = [
        "    .  .  .  .  .  .  .  .  .",
        "      .  .  .  .  .  .  .  . ",
        "    .  .  .  .  .  .  .  .  .",
        "      .  .  .  .  .  .  .  . ",
        "    .  .  .  .  .  .  .  .  .",
        "      .  .  .  .  .  .  .  . ",
        "    .  .  .  .  .  .  .  .  .",
        "      .  .  .  .  .  .  .  . ",
    ]
    bucket_line = "|" + "|".join(f"{lbl}" for lbl in _PLINKO_LBLS) + "|"
    pointer     = "  " + "     " * col + "🔴"
    return "```\n" + "\n".join(rows) + "\n" + pointer + "\n" + bucket_line + "\n```"


# ══════════════════════════════════════════════════════════════════════════════
# 📊  CASINO MENU  (updated with stats + new games)
# ══════════════════════════════════════════════════════════════════════════════

def _casino_menu_embed(uid: int) -> discord.Embed:
    _ensure_player(uid)
    s = _stats(uid)
    streak_txt = ""
    if s["streak"] >= 2:
        kind = "🔥 WIN" if s["streak_type"] == "win" else "❄️ LOSS"
        streak_txt = f"\n{kind} streak: **{s['streak']} games in a row!**"

    embed = discord.Embed(
        title="🎰  Gaming Zone Casino",
        description=(
            f"**Your Balance:** `{_wallet(uid):,}` PokeCoins{streak_txt}\n"
            f"*Bets: `{MIN_BET:,}` min — `{MAX_BET:,}` max*\n"
            f"⚡ Every win has a **7% chance** of a 🌟 **3× Bonus Round!**"
        ),
        color=0xFFD700,
    )
    embed.add_field(name="🎰 /slots <bet>",            value="3-reel machine. Match symbols for up to **50× your bet!**",             inline=False)
    embed.add_field(name="🃏 /blackjack <bet>",         value="Beat the dealer to 21. Hit · Stand · Double Down · Split. BJ = 1.5×!", inline=False)
    embed.add_field(name="🪙 /coinflip <bet>",          value="50/50 coin toss. Guess right → double your money.",                    inline=False)
    embed.add_field(name="🎡 /roulette <bet> <choice>", value="Wheel spin. Red/Black **1:1** · Green **17:1** · Number **35:1**",     inline=False)
    embed.add_field(name="🎲 /dice <bet>",              value="Roll higher than the bot. Tie = push.",                                inline=False)
    embed.add_field(name="🃏 /highlow <bet>",           value="Is the next card **higher** or **lower**? Correct = **1.9×**!",       inline=False)
    embed.add_field(name="🎯 /plinko <bet>",            value="Drop the ball through 8 rows of pegs. Up to **3× your bet!**",        inline=False)
    embed.add_field(name="🦹 /heist <bet> <target>",    value="Start a crew heist friends can join. Pick your target — up to **15× your bet!**",  inline=False)
    embed.add_field(name="📅 /daily",                   value=f"Claim **{DAILY_RANGE[0]}–{DAILY_RANGE[1]} free PokeCoins** every 24 hours!", inline=False)
    embed.add_field(name="💼 /work",                    value=f"Do a random game-themed job for **{WORK_RANGE[0]}–{WORK_RANGE[1]}** coins (1h cooldown)", inline=False)
    if s["games"] > 0:
        embed.add_field(
            name="📊 Your Casino Stats",
            value=(
                f"Games played: **{s['games']}**\n"
                f"Total won: **{s['won']:,}** · Total lost: **{s['lost']:,}** coins\n"
                f"Biggest single win: **{s['biggest_win']:,}** coins"
            ),
            inline=False,
        )
    embed.set_footer(text="🌟 7% chance of a 3× Bonus Round on ANY win  •  /casinomenu to see this again")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# SETUP — register all slash commands
# ══════════════════════════════════════════════════════════════════════════════

def setup_gambling(bot: commands.Bot) -> None:
    """Register all casino slash commands globally."""

    # Skip if already registered (check first command as marker)
    if bot.tree.get_command("casinomenu") is not None:
        return

    # ── /casinomenu ───────────────────────────────────────────────────────────
    @bot.tree.command(
        name="casinomenu",
        description="🎰 View all casino games, rules, and your stats",
    )
    async def cmd_casinomenu(interaction: discord.Interaction):
        _ensure_player(interaction.user.id)
        await interaction.response.send_message(
            embed=_casino_menu_embed(interaction.user.id)
        )

    # ── /setupcasino ──────────────────────────────────────────────────────────
    @bot.tree.command(
        name="setupcasino",
        description="🎰 Create a dedicated casino channel with pinned games menu (Admin only)",
    )
    async def cmd_setupcasino(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Administrator permission required.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # Create or reuse category
        cat = discord.utils.get(guild.categories, name="🎰 Casino")
        if not cat:
            cat = await guild.create_category("🎰 Casino")

        # Create or reuse text channel
        ch = discord.utils.get(guild.text_channels, name="🎰-casino-floor")
        if not ch:
            perms = {
                guild.default_role: discord.PermissionOverwrite(
                    send_messages=True,
                    read_messages=True,
                    read_message_history=True,
                )
            }
            ch = await guild.create_text_channel(
                "🎰-casino-floor",
                category=cat,
                overwrites=perms,
                topic=(
                    "Use all casino slash commands here! "
                    "/slots /blackjack /roulette /dice /heist /plinko /highlow /coinflip /daily"
                ),
            )

        # Post and pin the welcome embed
        pin_embed = discord.Embed(
            title="🎰  Welcome to the Gaming Zone Casino!",
            description=(
                "Bet your **PokeCoins** on any of these games:\n\n"
                "🎰 `/slots` — Spin the reels, up to **50×** payout!\n"
                "🃏 `/blackjack` — Beat the dealer! Hit · Stand · Double · Split\n"
                "🪙 `/coinflip` — 50/50 coin toss!\n"
                "🎡 `/roulette` — Spin the wheel, **35×** on a single number!\n"
                "🎲 `/dice` — Roll higher than the bot!\n"
                "🃏 `/highlow` — Higher or lower card? **1.9×** payout!\n"
                "🎯 `/plinko` — Drop the ball, up to **3×** payout!\n"
                "🦹 `/heist` — Start a joinable crew heist, up to **15×** payout!\n"
                "📅 `/daily` — Claim free coins every 24 hours!\n"
                "� `/work` — Do random game jobs for extra PokeCoins!\n"
                "�📊 `/casinomenu` — See all games & your personal stats!\n\n"
                "⚡ **Every win** has a **7% chance** of a 🌟 **3× BONUS ROUND!**\n"
                "*All bets use your PokeCoin wallet — earn more with `/daily` & Pokémon!*"
            ),
            color=0xFFD700,
        )
        pin_embed.set_footer(text="Good luck! 🍀  •  Min bet: 10  •  Max bet: 50,000")
        msg = await ch.send(embed=pin_embed)
        try:
            await msg.pin()
        except Exception:
            pass

        await interaction.followup.send(
            f"✅ Casino channel ready: {ch.mention}", ephemeral=True
        )

    # ── /daily ────────────────────────────────────────────────────────────────
    @bot.tree.command(
        name="daily",
        description="📅 Claim your free daily PokeCoins (24 h cooldown)",
    )
    async def cmd_daily(interaction: discord.Interaction):
        uid = interaction.user.id
        _ensure_player(uid)
        now  = time.time()
        last = _DAILY_CD.get(uid, 0)
        cd   = 86_400 - (now - last)
        if cd > 0:
            h = int(cd // 3600)
            m = int((cd % 3600) // 60)
            await interaction.response.send_message(
                f"⏳ Already claimed today! Come back in **{h}h {m}m**.",
                ephemeral=True,
            )
            return
        amount = random.randint(*DAILY_RANGE)
        WALLETS[uid] = _wallet(uid) + amount
        _DAILY_CD[uid] = now
        embed = discord.Embed(
            title="📅 Daily Reward Claimed!",
            description=f"**+{amount:,} PokeCoins** added to your wallet!\n\n{_bal_line(uid)}",
            color=0x2ECC71,
        )
        embed.set_footer(text="Come back in 24 hours for your next reward!  •  /slots /blackjack /roulette")
        await interaction.response.send_message(embed=embed)
        await _log_coin_event(interaction.guild, interaction.user, amount, "daily")

    # ── /work ─────────────────────────────────────────────────────────────────
    @bot.tree.command(
        name="work",
        description="💼 Do a random game-themed job to earn PokeCoins (1h cooldown)",
    )
    async def cmd_work(interaction: discord.Interaction):
        uid = interaction.user.id
        _ensure_player(uid)
        now = time.time()
        last = _WORK_CD.get(uid, 0)
        cd = WORK_COOLDOWN_SECONDS - (now - last)
        if cd > 0:
            m = int(cd // 60)
            s = int(cd % 60)
            await interaction.response.send_message(
                f"⏳ You already worked recently. Try again in **{m}m {s}s**.",
                ephemeral=True,
            )
            return

        amount = random.randint(*WORK_RANGE)
        WALLETS[uid] = _wallet(uid) + amount
        _WORK_CD[uid] = now
        event = random.choice(_WORK_EVENTS).format(coins=amount)

        embed = discord.Embed(
            title="💼 Shift Complete!",
            description=f"{event}\n\n{_bal_line(uid)}",
            color=0x2ECC71,
        )
        embed.set_footer(text="Use /work again after cooldown  •  /daily /slots /blackjack")
        await interaction.response.send_message(embed=embed)
        await _log_coin_event(interaction.guild, interaction.user, amount, "work")

    # ── /givepokcoin ───────────────────────────────────────────────────────────
    @bot.tree.command(
        name="givepokcoin",
        description="🛠️ Admin: give PokeCoins to a player",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member="Player to receive PokeCoins",
        amount="Amount of PokeCoins to give",
        reason="Optional reason for this grant",
    )
    async def cmd_givepokcoin(
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str = "Admin grant",
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Administrator permission required.",
                ephemeral=True,
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ Amount must be greater than 0.",
                ephemeral=True,
            )
            return

        _ensure_player(member.id)
        WALLETS[member.id] = _wallet(member.id) + amount

        embed = discord.Embed(
            title="🪙 PokeCoins Granted",
            color=0x3498DB,
            description=(
                f"{member.mention} received **{amount:,}** PokeCoins.\n"
                f"Reason: **{reason}**"
            ),
        )
        embed.add_field(name="👮 Granted by", value=interaction.user.mention, inline=True)
        embed.add_field(name="💼 New Balance", value=f"`{_wallet(member.id):,}` PokeCoins", inline=True)
        await interaction.response.send_message(embed=embed)

        await _log_coin_event(
            interaction.guild,
            member,
            amount,
            "admin gift",
            actor=interaction.user,
            reason=reason,
        )

    # ── /slots ────────────────────────────────────────────────────────────────
    @bot.tree.command(name="slots", description="🎰 Spin the slot machine")
    @app_commands.describe(bet="PokeCoins to bet")
    async def cmd_slots(interaction: discord.Interaction, bet: int):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        spin_embed, spin_img = _slots_embed_with_image(
            _slot_spin_box([]), bet, uid, spinning=True, revealed=[]
        )
        if spin_img:
            await interaction.response.send_message(embed=spin_embed, file=spin_img)
        else:
            await interaction.response.send_message(embed=spin_embed)

        # ── Determine reels ────────────────────────────────────────────────────
        if random.random() < 0.05:          # 5% hot streak: force jackpot
            lucky = _draw(uid)
            s1 = s2 = s3 = lucky
        else:
            s1, s2, s3 = _draw(uid), _draw(uid), _draw(uid)
            # Prevent accidental triple match outside hot streak
            if s1 == s2 == s3 and random.random() > 0.05:
                _USER_BASKETS.setdefault(uid, _fill_basket())
                _USER_BASKETS[uid].append(s3)
                random.shuffle(_USER_BASKETS[uid])
                s3 = _draw(uid)
                while s3 == s1 and len(set(_SLOT_SYMBOLS)) > 1:
                    _USER_BASKETS[uid].append(s3)
                    s3 = _draw(uid)

        # ── Reel-by-reel animation ─────────────────────────────────────────────
        await asyncio.sleep(0.8)
        spin_embed, spin_img = _slots_embed_with_image(
            _slot_spin_box([s1]), bet, uid, spinning=True, revealed=[s1]
        )
        if spin_img:
            await interaction.edit_original_response(embed=spin_embed, attachments=[spin_img])
        else:
            await interaction.edit_original_response(embed=spin_embed)
        await asyncio.sleep(0.7)
        spin_embed, spin_img = _slots_embed_with_image(
            _slot_spin_box([s1, s2]), bet, uid, spinning=True, revealed=[s1, s2]
        )
        if spin_img:
            await interaction.edit_original_response(embed=spin_embed, attachments=[spin_img])
        else:
            await interaction.edit_original_response(embed=spin_embed)
        await asyncio.sleep(0.7)

        # ── Resolve ────────────────────────────────────────────────────────────
        bonus = False
        if s1 == s2 == s3:
            base  = bet * _SLOT_MULT[s1]
            win, bonus = _bonus_roll(uid, base)
            WALLETS[uid] = _wallet(uid) + win
            _record_win(uid, win)
            result = (
                f"🎉 **JACKPOT! 3× {s1}** — Won **{win:,} PokeCoins**! (×{_SLOT_MULT[s1]})"
                + (" 🌟 **3× BONUS!**" if bonus else "")
            )
            color = 0xFFD700
        elif s1 == s2 or s2 == s3 or s1 == s3:
            match = s1 if (s1 == s2 or s1 == s3) else s2
            mult  = max(1, _SLOT_MULT[match] // 3)
            base  = bet * mult
            win, bonus = _bonus_roll(uid, base)
            WALLETS[uid] = _wallet(uid) + win
            _record_win(uid, win)
            result = (
                f"✨ **Partial {match}×2** — Won **{win:,} PokeCoins**! (×{mult})"
                + (" 🌟 **3× BONUS!**" if bonus else "")
            )
            color = 0x2ECC71
        else:
            WALLETS[uid] = _wallet(uid) - bet
            _record_loss(uid, bet)
            result = f"💸 **No match** — Lost **{bet:,}** PokeCoins."
            color  = 0xFF4444

        result_embed, result_img = _slots_embed_with_image(
            _slot_box(s1, s2, s3),
            bet,
            uid,
            result_text=result,
            color=color,
            bonus=bonus,
            revealed=[s1, s2, s3],
        )
        replay_view = PlayAgainView(uid, f"slots bet:{bet}", replay_action=lambda i, b=bet: cmd_slots.callback(i, b))
        if result_img:
            await interaction.edit_original_response(embed=result_embed, attachments=[result_img], view=replay_view)
        else:
            await interaction.edit_original_response(embed=result_embed, view=replay_view)

    # ── /blackjack ────────────────────────────────────────────────────────────
    @bot.tree.command(name="blackjack", description="🃏 Play Blackjack against the dealer")
    @app_commands.describe(bet="PokeCoins to bet")
    async def cmd_blackjack(interaction: discord.Interaction, bet: int):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        deck   = _new_deck()
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        pv     = _hand_value(player)
        if pv == 21:
            base = int(bet * 1.5)
            win, bonus = _bonus_roll(uid, base)
            WALLETS[uid] = _wallet(uid) + win
            _record_win(uid, win)
            embed, img_file = _bj_embed_with_image(
                player, dealer, bet, uid,
                f"🎉 **BLACKJACK!** Won **{win:,} PokeCoins**!"
                + (" 🌟 **3× BONUS!**" if bonus else ""),
                hide_dealer=False, bonus=bonus,
            )
            if img_file:
                await interaction.response.send_message(
                    embed=embed,
                    file=img_file,
                    view=PlayAgainView(uid, f"blackjack bet:{bet}", replay_action=lambda i, b=bet: cmd_blackjack.callback(i, b)),
                )
            else:
                await interaction.response.send_message(
                    embed=embed,
                    view=PlayAgainView(uid, f"blackjack bet:{bet}", replay_action=lambda i, b=bet: cmd_blackjack.callback(i, b)),
                )
            return
        view  = BlackjackView(uid, bet, player, dealer, deck, replay_action=lambda i, b=bet: cmd_blackjack.callback(i, b))
        embed, img_file = _bj_embed_with_image(player, dealer, bet, uid, "🎯 Your move!")
        if img_file:
            await interaction.response.send_message(embed=embed, view=view, file=img_file)
        else:
            await interaction.response.send_message(embed=embed, view=view)

    # ── /coinflip ─────────────────────────────────────────────────────────────
    @bot.tree.command(name="coinflip", description="🪙 Flip a coin — heads or tails!")
    @app_commands.describe(bet="PokeCoins to bet", choice="heads or tails")
    @app_commands.choices(choice=[
        app_commands.Choice(name="Heads 👑", value="heads"),
        app_commands.Choice(name="Tails 🦅", value="tails"),
    ])
    async def cmd_coinflip(interaction: discord.Interaction, bet: int, choice: str):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        spin_embed, spin_img = _coinflip_embed_with_image("", "", bet, uid, spinning=True)
        if spin_img:
            await interaction.response.send_message(embed=spin_embed, file=spin_img)
        else:
            await interaction.response.send_message(embed=spin_embed)
        await asyncio.sleep(1.8)
        result = random.choice(["heads", "tails"])
        bonus  = False
        if result == choice:
            win, bonus = _bonus_roll(uid, bet)
            WALLETS[uid] = _wallet(uid) + win
            _record_win(uid, win)
        else:
            WALLETS[uid] = _wallet(uid) - bet
            _record_loss(uid, bet)
        result_embed, result_img = _coinflip_embed_with_image(result, choice, bet, uid, bonus=bonus)
        replay_view = PlayAgainView(
            uid,
            f"coinflip bet:{bet} choice:{choice}",
            replay_action=lambda i, b=bet, c=choice: cmd_coinflip.callback(i, b, c),
        )
        if result_img:
            await interaction.edit_original_response(embed=result_embed, attachments=[result_img], view=replay_view)
        else:
            await interaction.edit_original_response(embed=result_embed, view=replay_view)

    # ── /roulette ─────────────────────────────────────────────────────────────
    @bot.tree.command(name="roulette", description="🎡 Spin the roulette wheel!")
    @app_commands.describe(
        bet="PokeCoins to bet",
        choice="red / black / green  OR  a number 0–36",
    )
    async def cmd_roulette(interaction: discord.Interaction, bet: int, choice: str):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        choice = choice.strip().lower()
        if choice not in {"red", "black", "green"}:
            try:
                n = int(choice)
                if not 0 <= n <= 36:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "❌ Pick `red`, `black`, `green`, or a number `0`–`36`.",
                    ephemeral=True,
                )
                return
        spin_seconds = 8
        spin_embed, spin_img = _roulette_embed_with_image(
            None,
            choice,
            bet,
            uid,
            spinning=True,
            spin_phase=0.0,
            countdown=spin_seconds,
        )
        if spin_img:
            await interaction.response.send_message(embed=spin_embed, file=spin_img)
        else:
            await interaction.response.send_message(embed=spin_embed)

        for remaining in range(spin_seconds - 1, 0, -1):
            await asyncio.sleep(1)
            phase = (spin_seconds - remaining) / spin_seconds
            spin_embed, spin_img = _roulette_embed_with_image(
                None,
                choice,
                bet,
                uid,
                spinning=True,
                spin_phase=phase,
                countdown=remaining,
            )
            if spin_img:
                await interaction.edit_original_response(embed=spin_embed, attachments=[spin_img])
            else:
                await interaction.edit_original_response(embed=spin_embed)

        landed = random.randint(0, 36)
        is_num = choice.lstrip("-").isdigit()
        if is_num:        won = landed == int(choice); mult = 35
        elif choice == "green":  won = landed == 0;         mult = 17
        elif choice == "red":    won = landed in _RED_NUMS;  mult = 1
        else:                    won = landed in _BLACK_NUMS; mult = 1
        bonus = False
        if won:
            payout, bonus = _bonus_roll(uid, bet * mult)
            WALLETS[uid]  = _wallet(uid) + payout
            _record_win(uid, payout)
        else:
            WALLETS[uid] = _wallet(uid) - bet
            _record_loss(uid, bet)
        result_embed, result_img = _roulette_embed_with_image(landed, choice, bet, uid, bonus=bonus)
        await interaction.edit_original_response(
            embed=result_embed,
            attachments=[result_img] if result_img else [],
            view=PlayAgainView(
                uid,
                f"roulette bet:{bet} choice:{choice}",
                replay_action=lambda i, b=bet, c=choice: cmd_roulette.callback(i, b, c),
            ),
        )

    # ── /dice ─────────────────────────────────────────────────────────────────
    @bot.tree.command(name="dice", description="🎲 Roll dice against the bot — highest wins!")
    @app_commands.describe(bet="PokeCoins to bet")
    async def cmd_dice(interaction: discord.Interaction, bet: int):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        spin_embed, spin_img = _dice_embed_with_image(None, None, bet, uid, spinning=True)
        if spin_img:
            await interaction.response.send_message(embed=spin_embed, file=spin_img)
        else:
            await interaction.response.send_message(embed=spin_embed)
        await asyncio.sleep(1.8)
        p_roll = random.randint(1, 6)
        b_roll = random.randint(1, 6)
        result_embed, result_img = _dice_embed_with_image(p_roll, b_roll, bet, uid)
        replay_view = PlayAgainView(uid, f"dice bet:{bet}", replay_action=lambda i, b=bet: cmd_dice.callback(i, b))
        if result_img:
            await interaction.edit_original_response(embed=result_embed, attachments=[result_img], view=replay_view)
        else:
            await interaction.edit_original_response(embed=result_embed, view=replay_view)

    # ── /highlow ──────────────────────────────────────────────────────────────
    @bot.tree.command(
        name="highlow",
        description="🃏 Guess if the next card is higher or lower! (1.9× payout)",
    )
    @app_commands.describe(bet="PokeCoins to bet", guess="Higher or lower than the current card?")
    @app_commands.choices(guess=[
        app_commands.Choice(name="Higher ⬆️", value="higher"),
        app_commands.Choice(name="Lower ⬇️",  value="lower"),
    ])
    async def cmd_highlow(interaction: discord.Interaction, bet: int, guess: str):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        deck    = _new_deck()
        current = deck.pop()
        spin_embed, spin_img = _hl_embed_with_image(current, None, guess, bet, uid, spinning=True)
        if spin_img:
            await interaction.response.send_message(embed=spin_embed, file=spin_img)
        else:
            await interaction.response.send_message(embed=spin_embed)
        await asyncio.sleep(1.5)
        next_card = deck.pop()
        cv = _hl_rank_val(current[0])
        nv = _hl_rank_val(next_card[0])
        bonus = False
        if cv == nv:
            result = "🤝 **PUSH** — Same value, bet returned."
            won    = None
        elif (guess == "higher" and nv > cv) or (guess == "lower" and nv < cv):
            win, bonus = _bonus_roll(uid, int(bet * 1.9))
            WALLETS[uid] = _wallet(uid) + win
            _record_win(uid, win)
            result = f"🎉 **CORRECT!** +**{win:,}** PokeCoins!" + (" 🌟 **3× BONUS!**" if bonus else "")
            won    = True
        else:
            WALLETS[uid] = _wallet(uid) - bet
            _record_loss(uid, bet)
            result = f"💸 **WRONG** — −**{bet:,}** PokeCoins."
            won    = False
        result_embed, result_img = _hl_embed_with_image(
            current,
            next_card,
            guess,
            bet,
            uid,
            won=bool(won),
            result=result,
            bonus=bonus,
        )
        replay_view = PlayAgainView(
            uid,
            f"highlow bet:{bet} guess:{guess}",
            replay_action=lambda i, b=bet, g=guess: cmd_highlow.callback(i, b, g),
        )
        if result_img:
            await interaction.edit_original_response(embed=result_embed, attachments=[result_img], view=replay_view)
        else:
            await interaction.edit_original_response(embed=result_embed, view=replay_view)

    # ── /plinko ───────────────────────────────────────────────────────────────
    @bot.tree.command(
        name="plinko",
        description="🎯 Drop the ball through the Plinko board! Up to 3× payout",
    )
    @app_commands.describe(bet="PokeCoins to bet")
    async def cmd_plinko(interaction: discord.Interaction, bet: int):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        spin_embed = discord.Embed(
            title="🎯 Plinko  ·  🌀 Ball dropping...",
            description=(
                "```\n"
                "          🔴\n"
                "    .  .  .  .  .  .  .  .  .\n"
                "      .  .  .  .  .  .  .  . \n"
                "    .  .  .  .  .  .  .  .  .\n"
                "          Dropping...\n"
                "```"
            ),
            color=0xFF6B35,
        )
        spin_img = _plinko_render_image_file(spinning=True)
        if spin_img:
            spin_embed.description = None
            spin_embed.set_image(url="attachment://plinko_board.png")
            await interaction.response.send_message(embed=spin_embed, file=spin_img)
        else:
            await interaction.response.send_message(embed=spin_embed)
        await asyncio.sleep(2.2)

        col, mult = _plinko_drop()
        base = int(bet * mult)
        bonus = False

        if base > bet:
            final, bonus = _bonus_roll(uid, base)
            gained = final - bet
            WALLETS[uid] = _wallet(uid) + gained
            _record_win(uid, gained)
            mult_lbl = f"{mult}×" + (" 🌟×3" if bonus else "")
            outcome  = f"🎉 **Won {final:,} PokeCoins!** ({mult_lbl})" + (" 🌟 **3× BONUS!**" if bonus else "")
            color    = 0xFFD700 if mult >= 3.0 else 0x2ECC71
        elif base == bet:
            final   = base
            outcome = f"🤝 **Broke even** — got back `{final:,}` coins. (×1.0)"
            color   = 0xFFAA00
        else:
            final  = base
            lost   = bet - final
            WALLETS[uid] = _wallet(uid) - lost
            _record_loss(uid, lost)
            outcome = f"💸 **Lost {lost:,} PokeCoins.** (×{mult})"
            color   = 0xFF4444

        result_embed = discord.Embed(
            title=f"🎯 {'🌟 BONUS! ' if bonus else ''}Plinko — Ball landed!",
            color=color,
        )
        result_embed.description = _plinko_board(col)
        result_embed.add_field(name="📊 Outcome",  value=outcome,                          inline=False)
        result_embed.add_field(name="💰 Bet",       value=f"`{bet:,}` PokeCoins",           inline=True)
        result_embed.add_field(name="💼 Balance",   value=f"`{_wallet(uid):,}` PokeCoins",  inline=True)
        if bonus:
            result_embed.add_field(name="⚡ CASINO BONUS", value="🎊 **3× MULTIPLIER** applied!", inline=False)
        result_embed.set_footer(text="🎯 /plinko  |  📅 /daily for free coins  |  📊 /casinomenu")
        result_img = _plinko_render_image_file(col=col, spinning=False)
        if result_img:
            result_embed.description = None
            result_embed.set_image(url="attachment://plinko_board.png")
            await interaction.edit_original_response(
                embed=result_embed,
                attachments=[result_img],
                view=PlayAgainView(uid, f"plinko bet:{bet}", replay_action=lambda i, b=bet: cmd_plinko.callback(i, b)),
            )
        else:
            await interaction.edit_original_response(
                embed=result_embed,
                view=PlayAgainView(uid, f"plinko bet:{bet}", replay_action=lambda i, b=bet: cmd_plinko.callback(i, b)),
            )

    # ── /heist ────────────────────────────────────────────────────────────────
    @bot.tree.command(
        name="heist",
        description="🦹 Plan and execute a multi-stage heist for big PokeCoins!",
    )
    @app_commands.describe(bet="PokeCoins to risk on the heist", target="Where to rob")
    @app_commands.choices(target=[
        app_commands.Choice(name="🏪 Corner Store   (Low risk · 1.5× reward)",      value="store"),
        app_commands.Choice(name="🏦 City Bank      (Medium risk · 3× reward)",      value="bank"),
        app_commands.Choice(name="💎 Diamond Vault  (High risk · 7× reward)",        value="vault"),
        app_commands.Choice(name="🚀 Federal Reserve (Extreme risk · 15× reward)",   value="fed"),
    ])
    async def cmd_heist(interaction: discord.Interaction, bet: int, target: str):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if interaction.channel_id in _HEIST_BUSY_CHANNELS:
            await interaction.response.send_message(
                "❌ A heist lobby is already active in this channel. Let that crew finish first.",
                ephemeral=True,
            )
            return

        _HEIST_BUSY_CHANNELS.add(interaction.channel_id)
        view = HeistLobbyView(interaction.user, bet, target)
        embed, lobby_img = _heist_lobby_embed(interaction.user, bet, target, [interaction.user])

        try:
            if lobby_img:
                embed.set_image(url="attachment://heist_panel.png")
                await interaction.response.send_message(embed=embed, file=lobby_img, view=view)
            else:
                await interaction.response.send_message(embed=embed, view=view)
            view.message = await interaction.original_response()
        except Exception:
            _HEIST_BUSY_CHANNELS.discard(interaction.channel_id)
            raise
