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

# ── Config ────────────────────────────────────────────────────────────────────
MIN_BET      = 10
MAX_BET      = 50_000
BONUS_CHANCE = 0.07          # 7 % chance of 3× bonus on any win
DAILY_RANGE  = (200, 600)    # min / max free daily coins

# ── Per-user casino stats ─────────────────────────────────────────────────────
# uid → {games, won, lost, biggest_win, streak, streak_type}
CASINO_STATS: dict[int, dict] = {}
_DAILY_CD:    dict[int, float] = {}   # uid → epoch of last /daily claim


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


def _bal_line(uid: int) -> str:
    return f"💼 **Balance:** `{_wallet(uid):,}` PokeCoins"


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

    card_w, card_h = 92, 132
    gap = 20
    side = 26
    top = 22
    row_gap = 42
    label_h = 22

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
        font_rank = ImageFont.truetype("DejaVuSans.ttf", 22)
        font_center = ImageFont.truetype("DejaVuSans.ttf", 30)
        font_label = ImageFont.truetype("DejaVuSans.ttf", 16)
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
        for spread, alpha in ((10, 52), (7, 78), (4, 110)):
            gd.rounded_rectangle(
                (x - spread, y - spread, x + card_w + spread, y + card_h + spread),
                radius=12 + spread,
                outline=(glow_rgb[0], glow_rgb[1], glow_rgb[2], alpha),
                width=2,
            )
        img.alpha_composite(glow)

    def draw_card(x: int, y: int, rank: str, suit: str, hidden: bool = False) -> None:
        if hidden:
            _draw_card_glow(x, y, (76, 190, 142))
            draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=11, fill=(26, 110, 79, 255), outline=(96, 208, 160, 255), width=3)
            draw.text((x + 31, y + 49), "🂠", fill=(228, 243, 235), font=font_center)
            return

        _draw_card_glow(x, y, (118, 225, 174))
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=11, fill=(248, 248, 248, 255), outline=(56, 176, 120, 255), width=3)
        c = suit_color(suit)
        suit_char = "♥" if "♥" in suit else "♦" if "♦" in suit else "♣" if "♣" in suit else "♠"
        draw.text((x + 9, y + 7), rank, fill=c, font=font_rank)
        draw.text((x + card_w // 2 - 10, y + card_h // 2 - 14), suit_char, fill=c, font=font_center)
        draw.text((x + card_w - 26, y + card_h - 30), rank, fill=c, font=font_rank)

    draw.text((side, top), "Dealer", fill=(226, 245, 235), font=font_label)
    y_dealer = top + label_h
    for i, (r, s) in enumerate(dealer):
        x = side + i * (card_w + gap)
        draw_card(x, y_dealer, r, s, hidden=(hide_dealer and i == 1))

    draw.text((side, y_dealer + card_h + row_gap - 18), "You", fill=(226, 245, 235), font=font_label)
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
    ):
        super().__init__(timeout=120)
        self.uid    = uid
        self.bet    = bet
        self.player = player
        self.dealer = dealer
        self.deck   = deck
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
                view=self,
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
            view=self,
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
    spinning: bool = False, bonus: bool = False,
) -> discord.Embed:
    if spinning:
        idx  = random.randint(0, 36)
        nums = [_WHEEL_ORDER[(idx + i) % len(_WHEEL_ORDER)] for i in range(5)]
        line = "  ".join(f"{_r_color(n)[0]}{n:02d}" for n in nums)
        return discord.Embed(
            title="🎡 Roulette  ·  🌀 Spinning...",
            description=f"```\n🎡  {line}  🎡\n    🌀🌀🌀🌀🌀🌀🌀\n```",
            color=0x5865F2,
        )
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


def _roulette_render_image_file(landed: int | None, spinning: bool = False) -> discord.File | None:
    if Image is None or ImageDraw is None or ImageFont is None:
        return None

    size = 768
    img = Image.new("RGBA", (size, size), (85, 120, 52, 255))
    draw = ImageDraw.Draw(img)

    cx = size // 2
    cy = 860  # push center off canvas to create curved wheel bands similar to reference
    outer_r = 700
    inner_r = 420
    seg_deg = 360.0 / len(_WHEEL_ORDER)

    target = landed if landed is not None else random.choice(_WHEEL_ORDER)
    target_idx = _WHEEL_ORDER.index(target)

    def seg_color(n: int) -> tuple[int, int, int]:
        if n == 0:
            return (19, 129, 86)
        if n in _RED_NUMS:
            return (204, 30, 36)
        return (18, 18, 22)

    # Center the landed segment near top-middle of the visible arc.
    base = -90.0 - ((target_idx + 0.5) * seg_deg)

    for i, n in enumerate(_WHEEL_ORDER):
        a0 = base + i * seg_deg
        a1 = a0 + seg_deg
        draw.pieslice((cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r), a0, a1, fill=seg_color(n))

    # Hollow center to form wheel band.
    draw.ellipse((cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r), fill=(85, 120, 52, 255))

    # White separators for each segment edge.
    mid_r = (outer_r + inner_r) // 2
    for i in range(len(_WHEEL_ORDER)):
        a = math.radians(base + i * seg_deg)
        x0 = int(cx + math.cos(a) * inner_r)
        y0 = int(cy + math.sin(a) * inner_r)
        x1 = int(cx + math.cos(a) * outer_r)
        y1 = int(cy + math.sin(a) * outer_r)
        draw.line((x0, y0, x1, y1), fill=(242, 242, 242, 245), width=3)

    # Show nearby numbers only (cleaner look, like screenshot).
    try:
        num_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except Exception:
        num_font = ImageFont.load_default()
    for offset in range(-2, 3):
        idx = (target_idx + offset) % len(_WHEEL_ORDER)
        n = _WHEEL_ORDER[idx]
        ang = math.radians(base + (idx + 0.5) * seg_deg)
        tx = int(cx + math.cos(ang) * mid_r)
        ty = int(cy + math.sin(ang) * mid_r)
        t = str(n)
        tw, th = draw.textbbox((0, 0), t, font=num_font)[2:4]
        draw.text((tx - tw // 2, ty - th // 2), t, fill=(232, 232, 232, 255), font=num_font)

    # Ball on landed segment (or randomized if spinning).
    ball_num = random.choice(_WHEEL_ORDER) if spinning else target
    ball_idx = _WHEEL_ORDER.index(ball_num)
    ball_ang = math.radians(base + (ball_idx + 0.5) * seg_deg)
    ball_r = inner_r + int((outer_r - inner_r) * 0.58)
    bx = int(cx + math.cos(ball_ang) * ball_r)
    by = int(cy + math.sin(ball_ang) * ball_r)
    br = 60
    draw.ellipse((bx - br, by - br, bx + br, by + br), fill=(200, 200, 200, 255), outline=(20, 20, 20, 255), width=3)
    draw.ellipse((bx - br + 18, by - br + 18, bx - br + 34, by - br + 34), fill=(236, 236, 236, 190))

    # Curved green rails above/below wheel for depth.
    draw.arc((cx - 760, cy - 760, cx + 760, cy + 760), start=196, end=346, fill=(146, 186, 88, 255), width=26)
    draw.arc((cx - 560, cy - 560, cx + 560, cy + 560), start=199, end=343, fill=(146, 186, 88, 255), width=24)

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
) -> tuple[discord.Embed, discord.File | None]:
    embed = _roulette_embed(landed, choice, bet, uid, spinning=spinning, bonus=bonus)
    img_file = _roulette_render_image_file(landed, spinning=spinning)
    if img_file:
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
    embed.add_field(name="🦹 /heist <bet> <target>",    value="Multi-stage crew heist. Pick your target — up to **15× your bet!**",  inline=False)
    embed.add_field(name="📅 /daily",                   value=f"Claim **{DAILY_RANGE[0]}–{DAILY_RANGE[1]} free PokeCoins** every 24 hours!", inline=False)
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
                "🦹 `/heist` — Multi-stage heist, up to **15×** payout!\n"
                "📅 `/daily` — Claim free coins every 24 hours!\n"
                "📊 `/casinomenu` — See all games & your personal stats!\n\n"
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

    # ── /slots ────────────────────────────────────────────────────────────────
    @bot.tree.command(name="slots", description="🎰 Spin the slot machine")
    @app_commands.describe(bet="PokeCoins to bet")
    async def cmd_slots(interaction: discord.Interaction, bet: int):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        await interaction.response.send_message(
            embed=_slots_embed(_slot_spin_box([]), bet, uid, spinning=True)
        )

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
        await interaction.edit_original_response(
            embed=_slots_embed(_slot_spin_box([s1]), bet, uid, spinning=True)
        )
        await asyncio.sleep(0.7)
        await interaction.edit_original_response(
            embed=_slots_embed(_slot_spin_box([s1, s2]), bet, uid, spinning=True)
        )
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

        await interaction.edit_original_response(
            embed=_slots_embed(
                _slot_box(s1, s2, s3), bet, uid,
                result_text=result, color=color, bonus=bonus,
            )
        )

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
                await interaction.response.send_message(embed=embed, file=img_file)
            else:
                await interaction.response.send_message(embed=embed)
            return
        view  = BlackjackView(uid, bet, player, dealer, deck)
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
        await interaction.response.send_message(
            embed=_coinflip_embed("", "", bet, uid, spinning=True)
        )
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
        await interaction.edit_original_response(
            embed=_coinflip_embed(result, choice, bet, uid, bonus=bonus)
        )

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
        spin_embed, spin_img = _roulette_embed_with_image(None, choice, bet, uid, spinning=True)
        if spin_img:
            await interaction.response.send_message(embed=spin_embed, file=spin_img)
        else:
            await interaction.response.send_message(embed=spin_embed)
        await asyncio.sleep(2.5)
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
        await interaction.response.send_message(
            embed=_dice_embed(None, None, bet, uid, spinning=True)
        )
        await asyncio.sleep(1.8)
        p_roll = random.randint(1, 6)
        b_roll = random.randint(1, 6)
        await interaction.edit_original_response(
            embed=_dice_embed(p_roll, b_roll, bet, uid)
        )

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
        await interaction.response.send_message(
            embed=_hl_embed(current, None, guess, bet, uid, spinning=True)
        )
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
        await interaction.edit_original_response(
            embed=_hl_embed(current, next_card, guess, bet, uid,
                            won=bool(won), result=result, bonus=bonus)
        )

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
        await interaction.edit_original_response(embed=result_embed)

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

        targets = {
            "store": {"name": "🏪 Corner Store",    "mult": 1.5,  "success_base": 0.75, "color": 0x2ECC71},
            "bank":  {"name": "🏦 City Bank",       "mult": 3.0,  "success_base": 0.50, "color": 0x3498DB},
            "vault": {"name": "💎 Diamond Vault",   "mult": 7.0,  "success_base": 0.30, "color": 0x9B59B6},
            "fed":   {"name": "🚀 Federal Reserve", "mult": 15.0, "success_base": 0.12, "color": 0xFF4444},
        }
        t = targets[target]

        # Phase 1 — Planning
        plan_embed = discord.Embed(
            title="🦹 Heist Planning Room",
            description=(
                f"**Target:** {t['name']}\n"
                f"**Potential payout:** `{int(bet * t['mult']):,}` PokeCoins  (**×{t['mult']}**)\n"
                f"**Base success chance:** `{int(t['success_base'] * 100)}%`\n\n"
                "```\n"
                "  🗺️  Studying the blueprints...\n"
                "  🔧  Sourcing equipment...\n"
                "  🕵️  Recruiting the crew...\n"
                "```"
            ),
            color=t["color"],
        )
        plan_embed.set_footer(text="Heist begins in 2 seconds...")
        await interaction.response.send_message(embed=plan_embed)
        await asyncio.sleep(2)

        # Phase 2 — Crew assembly
        crew_members = [
            ("🔓 Safecracker",   0.10),
            ("🚗 Getaway Driver", 0.08),
            ("💻 Hacker",         0.12),
            ("🔫 Muscle",         0.05),
            ("🕵️ Inside Man",    0.15),
        ]
        crew_lines  = []
        success_mod = 0.0
        for role, bonus_val in crew_members:
            if random.random() < 0.65:
                crew_lines.append(f"✅ {role} joined  (+{int(bonus_val * 100)}%)")
                success_mod += bonus_val
            else:
                crew_lines.append(f"❌ {role} bailed last minute")
        final_chance = min(t["success_base"] + success_mod, 0.92)

        crew_embed = discord.Embed(
            title="🦹 Crew Assembled",
            description="\n".join(crew_lines),
            color=t["color"],
        )
        crew_embed.add_field(
            name="📊 Final Success Chance", value=f"`{int(final_chance * 100)}%`", inline=True
        )
        crew_embed.add_field(name="💰 Bet", value=f"`{bet:,}` PokeCoins", inline=True)
        crew_embed.set_footer(text="Executing the heist...")
        await interaction.edit_original_response(embed=crew_embed)
        await asyncio.sleep(2.5)

        # Phase 3 — Heist stages
        stages = [
            ("🚨 Disabling the alarm",   "bypassed silently",        "alarm triggered!"),
            ("📦 Cracking the vault",    "cracked in seconds",        "silent alert sent"),
            ("🏃 Loading the loot",      "bags loaded, ready to go",  "guard spotted movement"),
            ("🚗 Making the getaway",    "vanished into the night",   "police gave chase"),
        ]
        stage_lines   = []
        heist_failed  = False
        for action, ok_txt, fail_txt in stages:
            if random.random() < final_chance:
                stage_lines.append(f"✅ **{action}** — {ok_txt}")
            else:
                stage_lines.append(f"❌ **{action}** — {fail_txt}!")
                heist_failed = True
                break

        # Phase 4 — Result
        succeeded = (random.random() < final_chance) and not heist_failed
        bonus     = False
        if succeeded:
            base    = int(bet * t["mult"])
            payout, bonus = _bonus_roll(uid, base)
            WALLETS[uid]  = _wallet(uid) + payout
            _record_win(uid, payout)
            color   = 0xFFD700 if bonus else 0x2ECC71
            outcome = (
                f"💰 **HEIST SUCCESSFUL!** The crew escaped clean!\n\n"
                f"**+{payout:,} PokeCoins**"
                + (" 🌟 **3× BONUS!**" if bonus else "")
            )
            title = f"🦹 {t['name']} — SUCCESS!"
        else:
            if random.random() < 0.30:
                recovered = int(bet * 0.4)
                WALLETS[uid] = _wallet(uid) - (bet - recovered)
                _record_loss(uid, bet - recovered)
                color   = 0xFFAA00
                outcome = (
                    f"⚠️ **PARTIAL ESCAPE!** Crew scattered — recovered some loot.\n\n"
                    f"**Lost {bet - recovered:,} PokeCoins** (recovered `{recovered:,}`)"
                )
                title = f"🦹 {t['name']} — PARTIAL"
            else:
                WALLETS[uid] = _wallet(uid) - bet
                _record_loss(uid, bet)
                color   = 0xFF4444
                outcome = (
                    f"🚨 **BUSTED!** Cops caught the crew and seized everything.\n\n"
                    f"**−{bet:,} PokeCoins** confiscated."
                )
                title = f"🦹 {t['name']} — BUSTED!"

        result_embed = discord.Embed(title=title, color=color)
        result_embed.add_field(
            name="📋 Heist Log",
            value="\n".join(stage_lines) or "Couldn't even get inside.",
            inline=False,
        )
        result_embed.add_field(name="📊 Outcome",  value=outcome,                          inline=False)
        result_embed.add_field(name="💰 Bet",       value=f"`{bet:,}` PokeCoins",           inline=True)
        result_embed.add_field(name="💼 Balance",   value=f"`{_wallet(uid):,}` PokeCoins",  inline=True)
        if bonus:
            result_embed.add_field(
                name="⚡ CASINO BONUS", value="🎊 **3× MULTIPLIER** applied to your payout!", inline=False
            )
        result_embed.set_footer(text="Plan your next heist with /heist!  |  /daily for free coins")
        await interaction.edit_original_response(embed=result_embed)
