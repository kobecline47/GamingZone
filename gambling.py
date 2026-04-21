"""
gambling.py — Casino mini-games powered by PokeCoin currency.
Games: /slots, /blackjack, /coinflip, /roulette
All wins and losses directly affect the PokeCoin wallet used to buy Pokemon.
"""

import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from pokemon_game import _wallet, _ensure_player, WALLETS

# ── Config ────────────────────────────────────────────────────────────────────
MIN_BET = 10
MAX_BET = 10_000

# ── Shared helpers ────────────────────────────────────────────────────────────

def _check_bet(uid: int, amount: int) -> str | None:
    """Returns an error string if the bet is invalid, otherwise None."""
    _ensure_player(uid)
    if amount < MIN_BET:
        return f"❌ Minimum bet is **{MIN_BET} PokeCoins**."
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


# ══════════════════════════════════════════════════════════════════════════════
# 🎰  SLOT MACHINE
# ══════════════════════════════════════════════════════════════════════════════

_SLOT_REELS: list[tuple[str, int]] = [
    ("🍒", 2),
    ("🍋", 3),
    ("🍊", 4),
    ("🍇", 5),
    ("⭐", 8),
    ("💎", 15),
    ("7️⃣", 25),
    ("🎰", 50),
]
_SLOT_SYMBOLS  = [s for s, _ in _SLOT_REELS]
_SLOT_WEIGHTS  = [30, 25, 20, 15, 5, 3, 1.5, 0.5]
_SLOT_MULT     = {s: m for s, m in _SLOT_REELS}


def _spin() -> str:
    return random.choices(_SLOT_SYMBOLS, weights=_SLOT_WEIGHTS, k=1)[0]


# ── Basket system ─────────────────────────────────────────────────────────────
# Each user gets a shuffled basket of outcomes so the same symbol can't spam.
# The basket is a deck: refills once empty. This prevents identical patterns
# repeating back-to-back while still feeling random.
_BASKET_SIZE = 30   # how many spins before refill
_USER_BASKETS: dict[int, list[str]] = {}  # uid -> remaining basket


def _fill_basket() -> list[str]:
    """Build a fresh shuffled basket weighted by rarity."""
    pool: list[str] = []
    # Each symbol gets a proportional count in the pool
    counts = [int(w) for w in _SLOT_WEIGHTS]  # [30,25,20,15,5,3,1,0] → round up
    for sym, count in zip(_SLOT_SYMBOLS, counts):
        pool.extend([sym] * max(count, 1))
    random.shuffle(pool)
    return pool


def _draw(uid: int) -> str:
    """Draw one symbol from the user's basket; refills when empty."""
    basket = _USER_BASKETS.get(uid)
    if not basket:
        basket = _fill_basket()
        _USER_BASKETS[uid] = basket
    return basket.pop()


def _slot_display(s1: str, s2: str, s3: str) -> str:
    return (
        "```\n"
        "╔═══════════════╗\n"
        f"║  ❓   ❓   ❓  ║\n"
        f"║▶ {s1}   {s2}   {s3} ◀║\n"
        f"║  ❓   ❓   ❓  ║\n"
        "╚═══════════════╝\n"
        "```"
    )


def _slots_embed(
    s1: str, s2: str, s3: str,
    bet: int, uid: int,
    result_text: str = "",
    color: int = 0x5865F2,
    spinning: bool = False,
) -> discord.Embed:
    title = "🎰 Slot Machine" + ("  —  🌀 Spinning..." if spinning else "")
    embed = discord.Embed(title=title, color=color)
    embed.description = _slot_display(s1, s2, s3)
    embed.add_field(name="💰 Bet", value=f"`{bet:,}` PokeCoins", inline=True)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    if result_text:
        embed.add_field(name="📊 Result", value=result_text, inline=False)
    if not spinning:
        embed.set_footer(text="Try again with /slots!")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# 🃏  BLACKJACK
# ══════════════════════════════════════════════════════════════════════════════

_RANKS  = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
_SUITS  = ["♠️", "♥️", "♦️", "♣️"]
_VALUES = {"A": 11, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
           "8": 8, "9": 9, "10": 10, "J": 10, "Q": 10, "K": 10}


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
    if hide_second and len(hand) >= 2:
        return f"`{hand[0][0]}{hand[0][1]}` `🂠 Hidden`"
    return "  ".join(f"`{r}{s}`" for r, s in hand)


def _bj_color(status: str) -> int:
    if any(w in status for w in ("WIN", "BLACKJACK")):
        return 0x2ECC71
    if any(w in status for w in ("LOSE", "BUST")):
        return 0xFF4444
    if "PUSH" in status:
        return 0xFFAA00
    return 0x5865F2


def _bj_embed(
    player: list, dealer: list, bet: int, uid: int,
    status: str = "", hide_dealer: bool = True,
) -> discord.Embed:
    pv = _hand_value(player)
    dv = _hand_value(dealer) if not hide_dealer else "?"
    embed = discord.Embed(title="🃏 Blackjack", color=_bj_color(status))
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
    embed.add_field(name="💰 Bet",     value=f"`{bet:,}` PokeCoins", inline=True)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    if status:
        embed.add_field(name="📊 Status", value=status, inline=False)
    if hide_dealer:
        embed.set_footer(text="Hit ▸ draw a card  |  Stand ▸ end your turn  |  Double ▸ 2× bet + 1 card")
    return embed


class BlackjackView(discord.ui.View):
    def __init__(self, uid: int, bet: int,
                 player: list, dealer: list, deck: list):
        super().__init__(timeout=120)
        self.uid    = uid
        self.bet    = bet
        self.player = player
        self.dealer = dealer
        self.deck   = deck

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

    async def _resolve(self, interaction: discord.Interaction, player_bust: bool = False) -> None:
        self._disable_all()
        self.stop()

        if player_bust:
            WALLETS[self.uid] = _wallet(self.uid) - self.bet
            status = f"💥 **BUST!** Over 21 — lost **{self.bet:,}** PokeCoins."
            embed  = _bj_embed(self.player, self.dealer, self.bet, self.uid, status, hide_dealer=False)
            await interaction.response.edit_message(embed=embed, view=self)
            return

        # Dealer draws to soft 17
        while _hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())

        pv, dv = _hand_value(self.player), _hand_value(self.dealer)

        if dv > 21 or pv > dv:
            WALLETS[self.uid] = _wallet(self.uid) + self.bet
            status = f"🏆 **WIN!** You won **{self.bet:,}** PokeCoins!"
        elif pv == dv:
            status = "🤝 **PUSH** — it's a tie, bet returned."
        else:
            WALLETS[self.uid] = _wallet(self.uid) - self.bet
            status = f"💸 **LOSE** — lost **{self.bet:,}** PokeCoins."

        embed = _bj_embed(self.player, self.dealer, self.bet, self.uid, status, hide_dealer=False)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ This isn't your game!", ephemeral=True)
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
            embed = _bj_embed(self.player, self.dealer, self.bet, self.uid,
                               "🎯 Card drawn — your move!", hide_dealer=True)
            await interaction.response.edit_message(embed=embed, view=self)

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
                "❌ Not enough PokeCoins to double down!", ephemeral=True
            )
            return
        self.bet *= 2
        self.player.append(self.deck.pop())
        pv = _hand_value(self.player)
        if pv > 21:
            await self._resolve(interaction, player_bust=True)
        else:
            await self._resolve(interaction)

    async def on_timeout(self) -> None:
        self._disable_all()


# ══════════════════════════════════════════════════════════════════════════════
# 🪙  COIN FLIP
# ══════════════════════════════════════════════════════════════════════════════

_FLIP_FRAMES = ["🌀", "🪙", "🌀", "🪙", "🌀"]


def _coinflip_embed(
    result: str, choice: str, bet: int, uid: int,
    spinning: bool = False,
) -> discord.Embed:
    if spinning:
        return discord.Embed(
            title="🪙 Coin Flip  —  🌀 Flipping...",
            description="The coin is in the air...",
            color=0xFFAA00,
        )
    won   = result == choice
    emoji = "👑" if result == "heads" else "🦅"
    color = 0x2ECC71 if won else 0xFF4444
    embed = discord.Embed(
        title=f"🪙 Coin Flip  —  {emoji} {result.upper()}!",
        color=color,
    )
    embed.add_field(name="Your Pick",  value=f"`{choice}`",         inline=True)
    embed.add_field(name="Result",     value=f"`{result}`",          inline=True)
    embed.add_field(name="\u200b",     value="\u200b",               inline=True)
    if won:
        embed.add_field(name="📊 Outcome",  value=f"🎉 **WON** `{bet:,}` PokeCoins!", inline=False)
    else:
        embed.add_field(name="📊 Outcome",  value=f"💸 **LOST** `{bet:,}` PokeCoins.", inline=False)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    embed.set_footer(text="Try again with /coinflip!")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# 🎡  ROULETTE
# ══════════════════════════════════════════════════════════════════════════════

_RED_NUMS   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
_BLACK_NUMS = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}


def _r_color(n: int) -> tuple[str, str]:
    if n == 0:
        return "🟢", "green"
    if n in _RED_NUMS:
        return "🔴", "red"
    return "⚫", "black"


def _roulette_embed(
    landed: int | None, choice: str, bet: int, uid: int,
    spinning: bool = False,
) -> discord.Embed:
    if spinning:
        return discord.Embed(
            title="🎡 Roulette  —  🌀 Spinning...",
            description="```\n  🎡  The wheel is spinning...  🎡\n```",
            color=0x5865F2,
        )
    assert landed is not None
    emoji, color_name = _r_color(landed)
    is_num = choice.lstrip("-").isdigit()
    if is_num:
        target_n = int(choice)
        won  = landed == target_n
        mult = 35
    elif choice == "green":
        won  = landed == 0
        mult = 17
    else:
        won  = (choice == "red" and landed in _RED_NUMS) or \
               (choice == "black" and landed in _BLACK_NUMS)
        mult = 1

    color = 0x2ECC71 if won else 0xFF4444
    embed = discord.Embed(title=f"🎡 Roulette  —  {emoji} **{landed}** ({color_name})", color=color)

    payout = bet * mult if won else 0
    outcome = f"🎉 **WIN!**  +**{payout:,}** PokeCoins  (×{mult})" if won else \
              f"💸 **LOSE**  −**{bet:,}** PokeCoins"
    embed.add_field(name="🎯 Ball Landed",  value=f"{emoji} `{landed}` ({color_name})", inline=True)
    embed.add_field(name="🎲 Your Bet",     value=f"`{choice}` → `{bet:,}` coins",      inline=True)
    embed.add_field(name="\u200b",          value="\u200b",                              inline=True)
    embed.add_field(name="📊 Outcome",      value=outcome,                               inline=False)
    embed.add_field(name="💼 Balance",      value=f"`{_wallet(uid):,}` PokeCoins",       inline=True)
    embed.set_footer(text="Red/Black pays 1:1  •  Green pays 17:1  •  Number pays 35:1")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# 🎲  DICE DUEL  (bonus game)
# ══════════════════════════════════════════════════════════════════════════════

_DICE = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]


def _dice_embed(
    p_roll: int | None, b_roll: int | None,
    bet: int, uid: int, spinning: bool = False,
) -> discord.Embed:
    if spinning:
        return discord.Embed(
            title="🎲 Dice Duel  —  🌀 Rolling...",
            description="```\n  🎲  Dice are rolling...  🎲\n```",
            color=0xFF6B35,
        )
    assert p_roll is not None and b_roll is not None
    pe, be = _DICE[p_roll - 1], _DICE[b_roll - 1]
    if p_roll > b_roll:
        color  = 0x2ECC71
        WALLETS[uid] = _wallet(uid) + bet
        result = f"🏆 **YOU WIN!** +**{bet:,}** PokeCoins!"
    elif p_roll == b_roll:
        color  = 0xFFAA00
        result = "🤝 **TIE** — bet returned."
    else:
        color  = 0xFF4444
        WALLETS[uid] = _wallet(uid) - bet
        result = f"💸 **YOU LOSE** — −**{bet:,}** PokeCoins."
    embed = discord.Embed(title="🎲 Dice Duel", color=color)
    embed.add_field(name="👤 You",     value=f"{pe} `{p_roll}`", inline=True)
    embed.add_field(name="🤖 Bot",     value=f"{be} `{b_roll}`", inline=True)
    embed.add_field(name="\u200b",     value="\u200b",           inline=True)
    embed.add_field(name="📊 Result",  value=result,             inline=False)
    embed.add_field(name="💼 Balance", value=f"`{_wallet(uid):,}` PokeCoins", inline=True)
    embed.set_footer(text="Roll higher than the bot to win!")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# 🏦  /casinomenu  — overview of all games
# ══════════════════════════════════════════════════════════════════════════════

def _casino_menu_embed(uid: int) -> discord.Embed:
    _ensure_player(uid)
    embed = discord.Embed(
        title="🎰 Gaming Zone Casino",
        description=(
            f"**Your Balance:** `{_wallet(uid):,}` PokeCoins\n"
            f"*Min bet: `{MIN_BET}` — Max bet: `{MAX_BET:,}`*\n\n"
            "All winnings/losses affect your **PokeCoin wallet** used to buy Pokemon!"
        ),
        color=0xFFD700,
    )
    embed.add_field(
        name="🎰 /slots <bet>",
        value="Spin 3 reels. Match symbols to win up to **50× your bet**!",
        inline=False,
    )
    embed.add_field(
        name="🃏 /blackjack <bet>",
        value="Beat the dealer to 21. Hit, Stand, or Double Down. Blackjack pays **1.5×**!",
        inline=False,
    )
    embed.add_field(
        name="🪙 /coinflip <bet> <heads|tails>",
        value="50/50 — guess right and double your bet.",
        inline=False,
    )
    embed.add_field(
        name="🎡 /roulette <bet> <red|black|green|0-36>",
        value="Spin the wheel. Red/Black pays **1:1**, Green **17:1**, Number **35:1**.",
        inline=False,
    )
    embed.add_field(
        name="🎲 /dice <bet>",
        value="Roll higher than the bot's dice. Tie = push.",
        inline=False,
    )
    embed.add_field(
        name="🦹 /heist <bet> <target>",
        value="Plan a multi-stage heist. Pick your target: Corner Store → Federal Reserve. Up to **15× your bet**!",
        inline=False,
    )
    embed.set_footer(text="Earn PokeCoins → use /pokeshop to buy Pokemon!")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════════════════════

def setup_gambling(bot: commands.Bot, guild_id: int) -> None:
    """Register all gambling slash commands."""
    gobj = discord.Object(id=guild_id)

    # ── /casinomenu ───────────────────────────────────────────────────────────
    @bot.tree.command(name="casinomenu", description="🎰 View all casino games and your PokeCoin balance", guild=gobj)
    async def cmd_casinomenu(interaction: discord.Interaction):
        _ensure_player(interaction.user.id)
        await interaction.response.send_message(embed=_casino_menu_embed(interaction.user.id))

    # ── /slots ────────────────────────────────────────────────────────────────
    @bot.tree.command(name="slots", description="🎰 Spin the slot machine with PokeCoins", guild=gobj)
    @app_commands.describe(bet="Amount of PokeCoins to bet")
    async def cmd_slots(interaction: discord.Interaction, bet: int):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        # Send spinning state
        await interaction.response.send_message(
            embed=_slots_embed("🌀", "🌀", "🌀", bet, uid, spinning=True)
        )
        await asyncio.sleep(1.2)

        # ── Basket draw — prevents same pattern back-to-back ──────────────────
        # ~5% chance of a hot streak: force all 3 reels to the same symbol
        if random.random() < 0.05:
            lucky = _draw(uid)
            s1 = s2 = s3 = lucky
        else:
            s1, s2, s3 = _draw(uid), _draw(uid), _draw(uid)
            # Prevent two identical consecutive draws being the same pair twice
            # by re-drawing s3 if all 3 accidentally match (keep it rare & earned)
            if s1 == s2 == s3 and random.random() > 0.05:
                _USER_BASKETS.setdefault(uid, _fill_basket())
                _USER_BASKETS[uid].append(s3)  # put it back
                random.shuffle(_USER_BASKETS[uid])
                # Draw a different symbol for s3
                s3 = _draw(uid)
                while s3 == s1 and len(set(_SLOT_SYMBOLS)) > 1:
                    _USER_BASKETS[uid].append(s3)
                    s3 = _draw(uid)

        if s1 == s2 == s3:
            mult  = _SLOT_MULT[s1]
            win   = bet * mult
            WALLETS[uid] = _wallet(uid) + win
            result = f"🎉 **JACKPOT! 3× {s1}** — Won **{win:,} PokeCoins**! (×{mult})"
            color  = 0xFFD700
        elif s1 == s2 or s2 == s3 or s1 == s3:
            match  = s1 if (s1 == s2 or s1 == s3) else s2
            mult   = max(1, _SLOT_MULT[match] // 3)
            win    = bet * mult
            WALLETS[uid] = _wallet(uid) + win
            result = f"✨ **Partial match {match}** — Won **{win:,} PokeCoins**! (×{mult})"
            color  = 0x2ECC71
        else:
            WALLETS[uid] = _wallet(uid) - bet
            result = f"💸 **No match** — Lost **{bet:,} PokeCoins**."
            color  = 0xFF4444

        await interaction.edit_original_response(
            embed=_slots_embed(s1, s2, s3, bet, uid, result_text=result, color=color)
        )

    # ── /blackjack ────────────────────────────────────────────────────────────
    @bot.tree.command(name="blackjack", description="🃏 Play Blackjack against the dealer", guild=gobj)
    @app_commands.describe(bet="Amount of PokeCoins to bet")
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

        # Natural blackjack
        if pv == 21:
            win = int(bet * 1.5)
            WALLETS[uid] = _wallet(uid) + win
            embed = _bj_embed(
                player, dealer, bet, uid,
                f"🎉 **BLACKJACK!** Won **{win:,} PokeCoins** (1.5× payout)!",
                hide_dealer=False,
            )
            await interaction.response.send_message(embed=embed)
            return

        view  = BlackjackView(uid, bet, player, dealer, deck)
        embed = _bj_embed(player, dealer, bet, uid, "🎯 Your move!", hide_dealer=True)
        await interaction.response.send_message(embed=embed, view=view)

    # ── /coinflip ─────────────────────────────────────────────────────────────
    @bot.tree.command(name="coinflip", description="🪙 Flip a coin — heads or tails!", guild=gobj)
    @app_commands.describe(bet="Amount of PokeCoins to bet", choice="heads or tails")
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
            embed=_coinflip_embed("", choice, bet, uid, spinning=True)
        )
        await asyncio.sleep(1.5)

        result = random.choice(["heads", "tails"])
        if result == choice:
            WALLETS[uid] = _wallet(uid) + bet
        else:
            WALLETS[uid] = _wallet(uid) - bet

        await interaction.edit_original_response(
            embed=_coinflip_embed(result, choice, bet, uid)
        )

    # ── /roulette ─────────────────────────────────────────────────────────────
    @bot.tree.command(name="roulette", description="🎡 Spin the roulette wheel!", guild=gobj)
    @app_commands.describe(
        bet="Amount of PokeCoins to bet",
        choice="red / black / green  OR  a number 0–36",
    )
    async def cmd_roulette(interaction: discord.Interaction, bet: int, choice: str):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        choice = choice.strip().lower()
        valid  = {"red", "black", "green"}
        if choice not in valid:
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

        await interaction.response.send_message(
            embed=_roulette_embed(None, choice, bet, uid, spinning=True)
        )
        await asyncio.sleep(2.0)

        landed = random.randint(0, 36)

        # Resolve payout
        is_num = choice.lstrip("-").isdigit()
        if is_num:
            won  = landed == int(choice)
            mult = 35
        elif choice == "green":
            won  = landed == 0
            mult = 17
        elif choice == "red":
            won  = landed in _RED_NUMS
            mult = 1
        else:
            won  = landed in _BLACK_NUMS
            mult = 1

        if won:
            WALLETS[uid] = _wallet(uid) + bet * mult
        else:
            WALLETS[uid] = _wallet(uid) - bet

        await interaction.edit_original_response(
            embed=_roulette_embed(landed, choice, bet, uid)
        )

    # ── /dice ─────────────────────────────────────────────────────────────────
    @bot.tree.command(name="dice", description="🎲 Roll dice vs the bot — highest roll wins!", guild=gobj)
    @app_commands.describe(bet="Amount of PokeCoins to bet")
    async def cmd_dice(interaction: discord.Interaction, bet: int):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        await interaction.response.send_message(
            embed=_dice_embed(None, None, bet, uid, spinning=True)
        )
        await asyncio.sleep(1.5)

        p_roll = random.randint(1, 6)
        b_roll = random.randint(1, 6)

        # Payout is handled inside _dice_embed
        embed = _dice_embed(p_roll, b_roll, bet, uid)
        await interaction.edit_original_response(embed=embed)

    # ── /heist ────────────────────────────────────────────────────────────────
    @bot.tree.command(name="heist", description="🦹 Plan and execute a multi-stage heist for big PokeCoins!", guild=gobj)
    @app_commands.describe(
        bet="PokeCoins to risk on the heist",
        target="Where to rob",
    )
    @app_commands.choices(target=[
        app_commands.Choice(name="🏪 Corner Store  (Low risk, low reward)",    value="store"),
        app_commands.Choice(name="🏦 City Bank     (Medium risk, 3× reward)",  value="bank"),
        app_commands.Choice(name="💎 Diamond Vault (High risk, 7× reward)",    value="vault"),
        app_commands.Choice(name="🚀 Federal Reserve (Extreme risk, 15× reward)", value="fed"),
    ])
    async def cmd_heist(interaction: discord.Interaction, bet: int, target: str):
        uid = interaction.user.id
        err = _check_bet(uid, bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        # ── Target config ─────────────────────────────────────────────────────
        targets = {
            "store": {"name": "🏪 Corner Store",    "mult": 1.5, "success_base": 0.75, "color": 0x2ECC71},
            "bank":  {"name": "🏦 City Bank",       "mult": 3.0, "success_base": 0.50, "color": 0x3498DB},
            "vault": {"name": "💎 Diamond Vault",   "mult": 7.0, "success_base": 0.30, "color": 0x9B59B6},
            "fed":   {"name": "🚀 Federal Reserve", "mult": 15.0,"success_base": 0.12, "color": 0xFF4444},
        }
        t = targets[target]

        # ── Phase 1: Planning embed ───────────────────────────────────────────
        plan_embed = discord.Embed(
            title="🦹 Heist Planning Room",
            description=(
                f"**Target:** {t['name']}\n"
                f"**Potential payout:** `{int(bet * t['mult']):,}` PokeCoins  (**×{t['mult']}**)\n"
                f"**Success chance:** `{int(t['success_base'] * 100)}%` base\n\n"
                "```\n"
                "  Assembling the crew...  🕵️\n"
                "  Studying the blueprints...  📐\n"
                "  Sourcing equipment...  🔧\n"
                "```"
            ),
            color=t["color"],
        )
        plan_embed.set_footer(text="The heist begins in 2 seconds...")
        await interaction.response.send_message(embed=plan_embed)
        await asyncio.sleep(2)

        # ── Phase 2: Crew roll ────────────────────────────────────────────────
        crew_members = [
            ("🔓 Safecracker",  0.10),
            ("🚗 Getaway Driver", 0.08),
            ("💻 Hacker",       0.12),
            ("🔫 Muscle",       0.05),
            ("🕵️ Inside Man",   0.15),
        ]
        crew_lines = []
        success_mod = 0.0
        for role, bonus in crew_members:
            joined = random.random() < 0.65
            if joined:
                crew_lines.append(f"✅ {role} joined the crew  (+{int(bonus*100)}%)")
                success_mod += bonus
            else:
                crew_lines.append(f"❌ {role} bailed last minute")

        final_chance = min(t["success_base"] + success_mod, 0.92)

        crew_embed = discord.Embed(
            title="🦹 Crew Assembled",
            description="\n".join(crew_lines),
            color=t["color"],
        )
        crew_embed.add_field(name="📊 Final Success Chance", value=f"`{int(final_chance * 100)}%`", inline=True)
        crew_embed.add_field(name="💰 Bet",                  value=f"`{bet:,}` PokeCoins",          inline=True)
        crew_embed.set_footer(text="Executing the heist...")
        await interaction.edit_original_response(embed=crew_embed)
        await asyncio.sleep(2.5)

        # ── Phase 3: Heist stages ─────────────────────────────────────────────
        stages = [
            ("🚨 Disabling the alarm system",    "bypassed the alarm",      "triggered the alarm early"),
            ("📦 Cracking open the vault",        "cracked it in seconds",   "set off a silent alert"),
            ("🏃 Loading the loot",               "loaded bags in record time","guard spotted the crew"),
            ("🚗 Making the getaway",             "vanished into the night", "cops gave chase"),
        ]
        stage_lines = []
        heist_failed = False
        for action, success_txt, fail_txt in stages:
            # Each stage independently can fail; failure at any stage ends the heist
            stage_ok = random.random() < final_chance
            if stage_ok:
                stage_lines.append(f"✅ **{action}** — {success_txt}")
            else:
                stage_lines.append(f"❌ **{action}** — {fail_txt}!")
                heist_failed = True
                break
            await asyncio.sleep(0.0)  # yield without delay (stages reveal at once)

        # Roll overall success
        rolled = random.random()
        succeeded = rolled < final_chance and not heist_failed

        # ── Phase 4: Result ───────────────────────────────────────────────────
        if succeeded:
            payout = int(bet * t["mult"])
            WALLETS[uid] = _wallet(uid) + payout
            color = 0xFFD700
            outcome = (
                f"💰 **HEIST SUCCESSFUL!**\n"
                f"The crew escaped clean with the loot!\n\n"
                f"**+{payout:,} PokeCoins** landed in your wallet!"
            )
            title = f"🦹 {t['name']} — SUCCESS!"
        else:
            # Partial recovery: sometimes escape with a fraction
            escape_roll = random.random()
            if escape_roll < 0.3:
                recovered = int(bet * 0.4)
                WALLETS[uid] = _wallet(uid) - (bet - recovered)
                color = 0xFFAA00
                outcome = (
                    f"⚠️ **PARTIAL ESCAPE!**\n"
                    f"The crew scattered — you recovered some loot.\n\n"
                    f"**Lost {bet - recovered:,} PokeCoins** (recovered `{recovered:,}`)"
                )
                title = f"🦹 {t['name']} — PARTIAL"
            else:
                WALLETS[uid] = _wallet(uid) - bet
                color = 0xFF4444
                outcome = (
                    f"🚨 **BUSTED!**\n"
                    f"The cops caught the crew and seized the loot.\n\n"
                    f"**−{bet:,} PokeCoins** confiscated."
                )
                title = f"🦹 {t['name']} — BUSTED!"

        result_embed = discord.Embed(title=title, color=color)
        result_embed.add_field(
            name="📋 Heist Log",
            value="\n".join(stage_lines) if stage_lines else "Couldn't even get inside.",
            inline=False,
        )
        result_embed.add_field(name="📊 Outcome",  value=outcome,                          inline=False)
        result_embed.add_field(name="💰 Bet",       value=f"`{bet:,}` PokeCoins",           inline=True)
        result_embed.add_field(name="💼 Balance",   value=f"`{_wallet(uid):,}` PokeCoins",  inline=True)
        result_embed.set_footer(text="Plan your next heist with /heist!")
        await interaction.edit_original_response(embed=result_embed)
