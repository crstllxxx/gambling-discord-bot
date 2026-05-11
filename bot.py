import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord import app_commands
from dotenv import load_dotenv


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("blackjack_bot")

DATA_DIR = Path("data")
BALANCES_FILE = DATA_DIR / "balances.json"
STARTING_BALANCE = 1_000
BLACKJACK_DEFAULT_BET = 100
CURRENCY_EMOJI = "💵"
DAILY_REWARD_AMOUNT = 50
DAILY_REWARD_COOLDOWN_SECONDS = 86_400

ROULETTE_RED_NUMBERS = {
    1,
    3,
    5,
    7,
    9,
    12,
    14,
    16,
    18,
    19,
    21,
    23,
    25,
    27,
    30,
    32,
    34,
    36,
}


@dataclass(frozen=True)
class Card:
    """Represents one playing card.

    Attributes:
        rank: Card rank symbol.
        suit: Card suit symbol.
    """

    rank: str
    suit: str

    @property
    def display(self) -> str:
        """Returns a readable card representation for Discord messages."""
        return f"{self.rank}{self.suit}"


@dataclass
class Deck:
    """Represents a shuffled 52-card deck for blackjack rounds."""

    cards: List[Card] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Populates and shuffles the deck after initialization."""
        suits = ["♠", "♥", "♦", "♣"]
        ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        self.cards = [Card(rank=rank, suit=suit) for suit in suits for rank in ranks]
        random.shuffle(self.cards)

    def draw(self) -> Card:
        """Draws one card from the deck.

        Raises:
            RuntimeError: If the deck is unexpectedly empty.
        """
        if not self.cards:
            raise RuntimeError("Колода пуста.")
        return self.cards.pop()


@dataclass
class Hand:
    """Stores cards for one blackjack hand and computes score."""

    cards: List[Card] = field(default_factory=list)

    def add_card(self, card: Card) -> None:
        """Adds one card to the hand.

        Args:
            card: Card to append.
        """
        self.cards.append(card)

    @property
    def score(self) -> int:
        """Calculates blackjack score with Ace soft/hard handling."""
        total = 0
        aces = 0

        for card in self.cards:
            if card.rank in {"J", "Q", "K"}:
                total += 10
            elif card.rank == "A":
                total += 11
                aces += 1
            else:
                total += int(card.rank)

        while total > 21 and aces > 0:
            total -= 10
            aces -= 1

        return total

    @property
    def is_blackjack(self) -> bool:
        """Checks if hand is a natural blackjack."""
        return len(self.cards) == 2 and self.score == 21

    @property
    def is_bust(self) -> bool:
        """Checks if hand exceeds score 21."""
        return self.score > 21

    def as_text(self, hide_first: bool = False) -> str:
        """Formats hand cards for embed display.

        Args:
            hide_first: If True, hides dealer first card.

        Returns:
            Formatted card list string.
        """
        if not self.cards:
            return "-"

        if hide_first:
            return "🂠 " + " ".join(card.display for card in self.cards[1:])

        return " ".join(card.display for card in self.cards)


@dataclass
class BlackjackGame:
    """Encapsulates blackjack game state and round transitions."""

    deck: Deck = field(default_factory=Deck)
    player_hand: Hand = field(default_factory=Hand)
    dealer_hand: Hand = field(default_factory=Hand)
    is_finished: bool = False
    result: str = ""

    def start(self) -> None:
        """Deals initial two cards for player and dealer."""
        self.player_hand.add_card(self.deck.draw())
        self.dealer_hand.add_card(self.deck.draw())
        self.player_hand.add_card(self.deck.draw())
        self.dealer_hand.add_card(self.deck.draw())

        if self.player_hand.is_blackjack:
            self.stand()

    def hit(self) -> None:
        """Draws a card for player and resolves bust state."""
        if self.is_finished:
            return

        self.player_hand.add_card(self.deck.draw())

        if self.player_hand.is_bust:
            self.is_finished = True
            self.result = "💥 Перебор! Вы проиграли."

    def stand(self) -> None:
        """Plays dealer turn and determines final outcome."""
        if self.is_finished:
            return

        while self.dealer_hand.score < 17:
            self.dealer_hand.add_card(self.deck.draw())

        self.is_finished = True

        player_score = self.player_hand.score
        dealer_score = self.dealer_hand.score

        if self.dealer_hand.is_bust:
            self.result = "🎉 Дилер перебрал. Вы победили!"
        elif player_score > dealer_score:
            self.result = "🎉 Вы победили!"
        elif player_score < dealer_score:
            self.result = "😢 Вы проиграли."
        else:
            self.result = "🤝 Ничья."


class EconomyManager:
    """Управляет балансами пользователей с хранением в JSON."""

    def __init__(self, storage_path: Path, starting_balance: int = STARTING_BALANCE) -> None:
        self.storage_path = storage_path
        self.starting_balance = starting_balance
        self._lock = asyncio.Lock()
        self._accounts = self._load()

    def _load(self) -> Dict[str, Dict[str, Optional[str]]]:
        if not self.storage_path.exists():
            return {}

        try:
            with self.storage_path.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
                if isinstance(raw, dict):
                    accounts: Dict[str, Dict[str, Optional[str]]] = {}
                    for key, value in raw.items():
                        if isinstance(value, dict):
                            balance = int(value.get("balance", self.starting_balance))
                            last_daily = value.get("last_daily")
                        else:
                            balance = int(value)
                            last_daily = None
                        accounts[str(key)] = {
                            "balance": balance,
                            "last_daily": last_daily,
                        }
                    return accounts
        except (json.JSONDecodeError, OSError, ValueError):
            logger.warning("Не удалось прочитать файл баланса, создаю новый.")
        return {}

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.storage_path.open("w", encoding="utf-8") as fp:
            json.dump(self._accounts, fp, ensure_ascii=False, indent=2)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _get_account(self, key: str) -> Dict[str, Optional[str]]:
        account = self._accounts.get(key)
        if account is None:
            account = {"balance": self.starting_balance, "last_daily": None}
            self._accounts[key] = account
        return account

    async def ensure_account(self, user_id: int) -> int:
        async with self._lock:
            account = self._get_account(str(user_id))
            self._save()
            return int(account["balance"])

    async def get_balance(self, user_id: int) -> int:
        async with self._lock:
            account = self._get_account(str(user_id))
            return int(account["balance"])

    async def change_balance(self, user_id: int, delta: int) -> int:
        async with self._lock:
            account = self._get_account(str(user_id))
            current = int(account["balance"])
            new_balance = max(0, current + delta)
            account["balance"] = new_balance
            self._save()
            return new_balance

    async def top_balances(self, limit: int = 10) -> List[tuple[int, int]]:
        async with self._lock:
            items = sorted(
                (
                    (int(user_id), int(account.get("balance", 0)))
                    for user_id, account in self._accounts.items()
                ),
                key=lambda entry: entry[1],
                reverse=True,
            )
            return items[:limit]

    async def claim_daily(self, user_id: int) -> tuple[bool, int, int]:
        """Пытается выдать ежедневную награду.

        Returns:
            (success, new_balance, remaining_seconds)
        """
        async with self._lock:
            account = self._get_account(str(user_id))
            last_daily_str = account.get("last_daily")
            now = self._now()

            if last_daily_str:
                try:
                    last_daily = datetime.fromisoformat(last_daily_str)
                except ValueError:
                    last_daily = None
                if last_daily:
                    elapsed = (now - last_daily).total_seconds()
                    if elapsed < DAILY_REWARD_COOLDOWN_SECONDS:
                        remaining = int(DAILY_REWARD_COOLDOWN_SECONDS - elapsed)
                        return False, int(account["balance"]), remaining

            account["balance"] = int(account["balance"]) + DAILY_REWARD_AMOUNT
            account["last_daily"] = now.isoformat()
            self._save()
            return True, int(account["balance"]), 0


def get_roulette_color(number: int) -> str:
    """Возвращает цвет сектора рулетки по числу."""
    if number == 0:
        return "зелёное"
    if number in ROULETTE_RED_NUMBERS:
        return "красное"
    return "чёрное"


def build_roulette_embed(
    player_name: str,
    bet: int,
    choice: str,
    spin_number: int,
    spin_color: str,
    outcome: str,
    payout: int,
    balance: int,
) -> discord.Embed:
    """Формирует embed с результатом раунда рулетки."""
    if payout > 0:
        color = discord.Color.green()
    elif payout < 0:
        color = discord.Color.red()
    else:
        color = discord.Color.blurple()

    embed = discord.Embed(
        title="🎡 Рулетка",
        description=f"Игрок: **{player_name}**",
        color=color,
    )
    embed.add_field(name="Ставка", value=f"**{bet}**", inline=True)
    embed.add_field(name="Выбор", value=f"**{choice}**", inline=True)
    embed.add_field(name="Выпало", value=f"**{spin_number} ({spin_color})**", inline=False)
    embed.add_field(name="Результат", value=outcome, inline=False)
    embed.add_field(
        name="Изменение баланса",
        value=f"{payout:+d} {CURRENCY_EMOJI}",
        inline=False,
    )
    embed.add_field(name="Текущий баланс", value=f"**{balance} {CURRENCY_EMOJI}**", inline=False)
    return embed


def calculate_blackjack_payout(game: BlackjackGame, bet: int) -> int:
    """Вычисляет изменение баланса по итогу раунда блэкджека."""
    if game.player_hand.is_bust:
        return -bet
    if game.dealer_hand.is_bust:
        return bet
    if game.player_hand.score > game.dealer_hand.score:
        return bet
    if game.player_hand.score < game.dealer_hand.score:
        return -bet
    return 0


def format_hand_with_emojis(hand: Hand) -> str:
    """Формирует строку карт с эмодзи как в примере."""
    if not hand.cards:
        return "-"
    # Здесь можно заменить на настоящие эмодзи колоды, пока используем ♠♥♦♣
    return " ".join(card.display for card in hand.cards)


def build_game_embed(
    game: BlackjackGame,
    player_name: str,
    bet: Optional[int] = None,
    avatar_url: Optional[str] = None,
    balance: Optional[int] = None,
) -> discord.Embed:
    """Builds a Discord embed with the current blackjack state.

    Args:
        game: Current game object.
        player_name: Display name of the player.

    Returns:
        Discord embed for response editing/sending.
    """
    reveal_dealer = game.is_finished

    embed = discord.Embed(
        title="🃏 Блэкджек",
        color=discord.Color.dark_purple(),
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    if game.is_finished and bet is not None:
        payout = calculate_blackjack_payout(game, bet)
        amount_text = f"{abs(payout)} {CURRENCY_EMOJI}"
        if payout > 0:
            description = f"🎉 **{player_name}**, вы победили!"
            description += f"\nВы получили: **{amount_text}**."
        elif payout < 0:
            description = "😢 Дилер победил игрока!"
            description += f"\nВы проиграли: **{amount_text}**."
        else:
            description = f"🤝 Ничья для игрока {player_name}."
    else:
        description = f"**{player_name}**, возьмите карту или остановитесь."

    embed.description = description

    embed.add_field(
        name=f"Карты игрока ({game.player_hand.score})",
        value=format_hand_with_emojis(game.player_hand),
        inline=True,
    )

    dealer_score_text = str(game.dealer_hand.score) if reveal_dealer else "?"
    embed.add_field(
        name=f"Карты дилера ({dealer_score_text})",
        value=format_hand_with_emojis(game.dealer_hand) if reveal_dealer else "🂠",
        inline=True,
    )

    if bet is not None:
        embed.add_field(name="Ставка", value=f"**{bet} {CURRENCY_EMOJI}**", inline=False)
    if balance is not None:
        embed.add_field(name="Баланс", value=f"**{balance} {CURRENCY_EMOJI}**", inline=False)

    if game.is_finished:
        embed.add_field(name="Результат", value=game.result, inline=False)
    else:
        embed.set_footer(text="Нажмите «Взять», чтобы взять карту, или «Стоп», чтобы остановиться.")

    return embed


class BlackjackView(discord.ui.View):
    """Interactive Discord view with hit/stand controls for one player."""

    def __init__(
        self,
        player_id: int,
        player_name: str,
        game: BlackjackGame,
        bet: int,
        economy_manager: EconomyManager,
        timeout: int = 120,
    ) -> None:
        """Initializes button view for one active blackjack round.

        Args:
            player_id: Discord user ID allowed to press buttons.
            player_name: Display name used in embed.
            game: Active blackjack game instance.
            timeout: Seconds before controls are disabled.
        """
        super().__init__(timeout=timeout)
        self.player_id = player_id
        self.player_name = player_name
        self.game = game
        self.bet = bet
        self.economy = economy_manager
        self._payout_applied = False

    async def on_timeout(self) -> None:
        """Disables controls after timeout and logs session expiration."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        logger.info("Сессия блэкджека истекла для player_id=%s", self.player_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensures only the game owner can use buttons.

        Args:
            interaction: Incoming component interaction.

        Returns:
            True when interaction belongs to game owner.
        """
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "Эта игра принадлежит другому пользователю.",
                ephemeral=True,
            )
            return False
        return True

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        """Refreshes game message after state change.

        Args:
            interaction: Button interaction to update.
        """
        if self.game.is_finished:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

        updated_balance = await self.economy.ensure_account(self.player_id)
        avatar_url = interaction.user.display_avatar.url if interaction.user else None
        await interaction.response.edit_message(
            embed=build_game_embed(
                self.game,
                self.player_name,
                self.bet,
                avatar_url=avatar_url,
                balance=updated_balance,
            ),
            view=self,
        )
        await self._apply_payout_if_needed(interaction)

    @discord.ui.button(label="Взять", style=discord.ButtonStyle.primary)
    async def hit_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Handles draw-card button action with error-safe response."""
        try:
            self.game.hit()
            await self._refresh_message(interaction)
        except Exception as error:
            logger.exception("Не удалось обработать действие «Взять»: %s", error)
            if interaction.response.is_done():
                await interaction.followup.send("Ошибка при обработке хода.", ephemeral=True)
            else:
                await interaction.response.send_message("Ошибка при обработке хода.", ephemeral=True)

    @discord.ui.button(label="Стоп", style=discord.ButtonStyle.success)
    async def stand_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Handles stand button action with error-safe response."""
        try:
            self.game.stand()
            await self._refresh_message(interaction)
        except Exception as error:
            logger.exception("Не удалось обработать действие «Стоп»: %s", error)
            if interaction.response.is_done():
                await interaction.followup.send("Ошибка при завершении игры.", ephemeral=True)
            else:
                await interaction.response.send_message("Ошибка при завершении игры.", ephemeral=True)

    async def _apply_payout_if_needed(self, interaction: discord.Interaction) -> None:
        if not self.game.is_finished or self._payout_applied:
            return

        self._payout_applied = True
        payout = self._calculate_payout()

        if payout != 0:
            new_balance = await self.economy.change_balance(self.player_id, payout)
        else:
            new_balance = await self.economy.ensure_account(self.player_id)

        if payout > 0:
            message = f"🎉 Вы выиграли {payout} монет. Баланс: {new_balance}."
        elif payout < 0:
            message = f"😢 Вы проиграли {abs(payout)} монет. Баланс: {new_balance}."
        else:
            message = f"🤝 Ничья. Баланс без изменений: {new_balance}."

        await interaction.followup.send(message, ephemeral=True)

    def _calculate_payout(self) -> int:
        if "победили" in self.game.result and "проиграли" not in self.game.result:
            return self.bet
        if "проиграли" in self.game.result or "Перебор" in self.game.result:
            return -self.bet
        return 0


class BlackjackBot(discord.Client):
    """Discord client с мини-играми и экономикой."""

    def __init__(self) -> None:
        """Configures client with minimal required intents and command tree."""
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.economy = EconomyManager(BALANCES_FILE)
        admin_ids_env = os.getenv("DISCORD_ADMIN_IDS", "")
        self.admin_ids = {
            int(value)
            for value in (part.strip() for part in admin_ids_env.split(","))
            if value.isdigit()
        }

    async def setup_hook(self) -> None:
        """Registers and syncs application commands on startup."""
        self._register_commands()
        await self.tree.sync()
        logger.info("Команды приложения синхронизированы.")

    def _register_commands(self) -> None:
        """Defines slash commands for blackjack game workflow."""

        @self.tree.command(name="blackjack", description="Запустить мини-игру в блэкджек")
        @app_commands.describe(bet="Размер ставки (целое положительное число)")
        async def blackjack(
            interaction: discord.Interaction,
            bet: app_commands.Range[int, 1, 100_000] = BLACKJACK_DEFAULT_BET,
        ) -> None:
            """Starts a new blackjack game for the caller.

            Args:
                interaction: Slash command interaction context.
            """
            try:
                balance = await self.economy.ensure_account(interaction.user.id)
                if balance < bet:
                    await interaction.response.send_message(
                        f"Недостаточно монет. Ваш баланс: {balance}.",
                        ephemeral=True,
                    )
                    return

                game = BlackjackGame()
                game.start()
                view = BlackjackView(
                    player_id=interaction.user.id,
                    player_name=interaction.user.display_name,
                    game=game,
                    bet=bet,
                    economy_manager=self.economy,
                )

                avatar_url = interaction.user.display_avatar.url if interaction.user else None
                await interaction.response.send_message(
                    embed=build_game_embed(
                        game,
                        interaction.user.display_name,
                        bet,
                        avatar_url=avatar_url,
                        balance=balance - bet,
                    ),
                    view=view,
                )
                await view._apply_payout_if_needed(interaction)  # Natural blackjack сразу завершает игру
            except Exception as error:
                logger.exception("Не удалось запустить игру в блэкджек: %s", error)
                if interaction.response.is_done():
                    await interaction.followup.send("Не удалось запустить игру.", ephemeral=True)
                else:
                    await interaction.response.send_message("Не удалось запустить игру.", ephemeral=True)

        @self.tree.command(name="roulette", description="Сделать ставку в рулетке")
        @app_commands.describe(
            bet="Размер ставки (целое положительное число)",
            choice="На что ставите: цвет или число",
            number="Число от 0 до 36 (только если выбор: число)",
        )
        @app_commands.choices(
            choice=[
                app_commands.Choice(name="Красное", value="красное"),
                app_commands.Choice(name="Чёрное", value="чёрное"),
                app_commands.Choice(name="Зелёное", value="зелёное"),
                app_commands.Choice(name="Число", value="число"),
            ]
        )
        async def roulette(
            interaction: discord.Interaction,
            bet: app_commands.Range[int, 1, 1_000_000],
            choice: str,
            number: Optional[int] = None,
        ) -> None:
            """Запускает раунд рулетки и определяет выигрыш по ставке."""
            try:
                if choice == "число":
                    if number is None:
                        await interaction.response.send_message(
                            "Для ставки на число укажите параметр `number` от 0 до 36.",
                            ephemeral=True,
                        )
                        return
                    if not 0 <= number <= 36:
                        await interaction.response.send_message(
                            "Число должно быть в диапазоне от 0 до 36.",
                            ephemeral=True,
                        )
                        return
                elif number is not None:
                    await interaction.response.send_message(
                        "Параметр `number` используется только при выборе `число`.",
                        ephemeral=True,
                    )
                    return

                balance = await self.economy.ensure_account(interaction.user.id)
                if balance < bet:
                    await interaction.response.send_message(
                        f"Недостаточно монет. Ваш баланс: {balance}.",
                        ephemeral=True,
                    )
                    return

                spin_number = random.randint(0, 36)
                spin_color = get_roulette_color(spin_number)

                is_win = False
                payout = -bet

                if choice == "число":
                    if spin_number == number:
                        is_win = True
                        payout = bet * 35
                elif choice == spin_color:
                    is_win = True
                    if choice == "зелёное":
                        payout = bet * 35
                    else:
                        payout = bet

                if is_win:
                    outcome = "🎉 Ставка сыграла!"
                else:
                    outcome = "😢 Ставка не сыграла."

                choice_text = choice if choice != "число" else f"число {number}"

                new_balance = await self.economy.change_balance(interaction.user.id, payout)

                await interaction.response.send_message(
                    embed=build_roulette_embed(
                        player_name=interaction.user.display_name,
                        bet=bet,
                        choice=choice_text,
                        spin_number=spin_number,
                        spin_color=spin_color,
                        outcome=outcome,
                        payout=payout,
                        balance=new_balance,
                    )
                )
            except Exception as error:
                logger.exception("Не удалось запустить игру в рулетку: %s", error)
                if interaction.response.is_done():
                    await interaction.followup.send("Не удалось запустить рулетку.", ephemeral=True)
                else:
                    await interaction.response.send_message("Не удалось запустить рулетку.", ephemeral=True)

        @self.tree.command(name="balance", description="Показать текущий баланс монет")
        async def balance_command(interaction: discord.Interaction) -> None:
            amount = await self.economy.ensure_account(interaction.user.id)
            await interaction.response.send_message(
                f"Ваш баланс: **{amount}** монет.",
                ephemeral=True,
            )

        @self.tree.command(name="leaderboard", description="Топ игроков по количеству монет")
        async def leaderboard(interaction: discord.Interaction) -> None:
            top = await self.economy.top_balances()
            if not top:
                description = "Пока нет данных."
            else:
                lines = [
                    f"{idx}. <@{user_id}> — **{amount}** монет"
                    for idx, (user_id, amount) in enumerate(top, start=1)
                ]
                description = "\n".join(lines)

            embed = discord.Embed(
                title="🏆 Лидеры по монетам",
                description=description,
                color=discord.Color.purple(),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="daily", description="Ежедневный бонус монет")
        async def daily(interaction: discord.Interaction) -> None:
            success, balance_amount, remaining = await self.economy.claim_daily(interaction.user.id)
            if success:
                await interaction.response.send_message(
                    f"Вы получили ежедневные **{DAILY_REWARD_AMOUNT} {CURRENCY_EMOJI}**! Баланс: {balance_amount} {CURRENCY_EMOJI}.",
                    ephemeral=True,
                )
            else:
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                seconds = remaining % 60
                await interaction.response.send_message(
                    f"Бонус уже получен. Повторно можно через {hours:02d}:{minutes:02d}:{seconds:02d}.",
                    ephemeral=True,
                )

        @self.tree.command(name="pay", description="Перевести монеты другому пользователю")
        @app_commands.describe(user="Получатель", amount="Сумма перевода")
        async def pay(
            interaction: discord.Interaction,
            user: discord.User,
            amount: app_commands.Range[int, 1, 1_000_000],
        ) -> None:
            if user.id == interaction.user.id:
                await interaction.response.send_message(
                    "Нельзя переводить монеты самому себе.",
                    ephemeral=True,
                )
                return

            sender_balance = await self.economy.ensure_account(interaction.user.id)
            await self.economy.ensure_account(user.id)

            if sender_balance < amount:
                await interaction.response.send_message(
                    f"Недостаточно монет. Ваш баланс: {sender_balance}.",
                    ephemeral=True,
                )
                return

            new_sender_balance = await self.economy.change_balance(interaction.user.id, -amount)
            await self.economy.change_balance(user.id, amount)

            await interaction.response.send_message(
                f"Вы перевели <@{user.id}> **{amount}** монет. Ваш баланс: {new_sender_balance}.",
                ephemeral=True,
            )

        @self.tree.command(name="grant", description="(Админ) начислить монеты себе")
        @app_commands.describe(amount="Размер начисления")
        async def grant(
            interaction: discord.Interaction,
            amount: app_commands.Range[int, 1, 100_000_000],
        ) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.",
                    ephemeral=True,
                )
                return

            new_balance = await self.economy.change_balance(interaction.user.id, amount)
            await interaction.response.send_message(
                f"Вы начислили себе {amount} монет. Новый баланс: {new_balance}.",
                ephemeral=True,
            )

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


async def run_bot() -> None:
    """Loads environment, validates configuration, and starts Discord bot.

    Raises:
        RuntimeError: If required token is missing in environment.
    """
    load_dotenv()
    token: Optional[str] = os.getenv("DISCORD_BOT_TOKEN")

    if not token:
        raise RuntimeError("Требуется переменная окружения DISCORD_BOT_TOKEN.")

    bot = BlackjackBot()
    await bot.start(token)


if __name__ == "__main__":
    try:
        import asyncio

        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")
    except Exception as startup_error:
        logger.exception("Не удалось запустить бота: %s", startup_error)
