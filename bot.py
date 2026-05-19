import asyncio
import functools
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

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
SHOP_FILE = DATA_DIR / "shop.json"
TOTALS_FILE = DATA_DIR / "totals.json"
FARM_FILE = DATA_DIR / "farm.json"

# Настройки фарма за сообщения
FARM_MESSAGES_REQUIRED = 30  # Сообщений для получения награды
FARM_REWARD = 5  # Награда в рублях

# Настройки тотализатора (ставки на события)
TOTALS_DEFAULT_MIN_BET = 100  # Минимальная ставка
TOTALS_DEFAULT_MAX_BET = 10_000  # Максимальная ставка
STARTING_BALANCE = 1_000
BLACKJACK_DEFAULT_BET = 100
CURRENCY_EMOJI = "💵"
DAILY_REWARD_AMOUNT = 50
DAILY_REWARD_COOLDOWN_SECONDS = 86_400

# Магазин
SHOP_ITEMS = {
    "999_rubles": {
        "name": "999 рублей",
        "description": "Выгодное предложение: получите 999 рублей за 1000 рублей!",
        "price": 1000,
        "emoji": "💸",
        "reward": 999,
    },
    "lottery_ticket": {
        "name": "Лотерейный билет",
        "description": "Участие в розыгрыше джекпота",
        "price": 20000,
        "emoji": "🎫",
    },
    "daily_booster": {
        "name": "Удвоитель бонуса (ПЕРМАНЕНТНЫЙ)",
        "description": "⚡ НАВСЕГДА удваивает ежедневный бонус! 100 рублей вместо 50!",
        "price": 30000,
        "emoji": "⚡",
        "multiplier": 2,
        "permanent": True,
    },
    "custom_emoji": {
        "name": "Кастомный эмодзи-слот",
        "description": "Добавь свой кастомный эмодзи на сервер (требует одобрения админов)",
        "price": 60000,
        "emoji": "😀",
    },
    "custom_role": {
        "name": "Кастомная роль",
        "description": "Создай свою уникальную роль (согласовано с админами)",
        "price": 65000,
        "emoji": "🎨",
    },
    "nickname_change": {
        "name": "📝 Смена ника другу",
        "description": "Переименуй любого пользователя на сервере!",
        "price": 67000,
        "emoji": "📝",
    },
    "server_avatar_2d": {
        "name": "Смена аватарки сервера (на 24 часа)",
        "description": "Установи любую аватарку сервера на 24 часа",
        "price": 75000,
        "emoji": "🖼️",
        "duration_hours": 24,
    },
    "joke_mute": {
        "name": "🔇 Мут «ради шутки»",
        "description": "Замути друга на 5 минут ради шутки!",
        "price": 100000,
        "emoji": "🔇",
        "duration_seconds": 300,
    },
}

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


@dataclass
class CrashGame:
    """Игра Crash с растущим множителем."""

    crash_point: float = field(init=False)
    start_time: float = field(init=False)
    is_finished: bool = False
    is_crashed: bool = False
    cashed_out: bool = False
    cashout_multiplier: float = 0.0

    # Параметры игры
    GROWTH_RATE: float = 0.05  # +0.05x в секунду
    MIN_CRASH: float = 1.01
    MAX_CRASH: float = 10.0

    def __post_init__(self):
        """Генерирует точку краша при создании игры."""
        self.start_time = asyncio.get_event_loop().time()
        # Экспоненциальное распределение для краша (более реалистично)
        # Среднее ~1.8x с повышенным параметром для более частых крашей (рискованная игра)
        self.crash_point = random.expovariate(1.4) + self.MIN_CRASH
        if self.crash_point > self.MAX_CRASH:
            self.crash_point = self.MAX_CRASH

    def get_current_multiplier(self) -> float:
        """Возвращает текущий множитель."""
        if self.is_finished:
            return self.crash_point if self.is_crashed else self.cashout_multiplier

        elapsed = asyncio.get_event_loop().time() - self.start_time
        current = 1.0 + (elapsed * self.GROWTH_RATE)
        return round(current, 2)

    def check_crash(self) -> bool:
        """Проверяет, произошёл ли краш."""
        if self.is_finished:
            return self.is_crashed

        current = self.get_current_multiplier()
        if current >= self.crash_point:
            self.is_finished = True
            self.is_crashed = True
            return True
        return False

    def cash_out(self) -> float:
        """Забирает выигрыш. Возвращает множитель или 0 если уже краш."""
        if self.is_finished:
            return 0.0

        current = self.get_current_multiplier()
        if current >= self.crash_point:
            self.is_finished = True
            self.is_crashed = True
            return 0.0

        self.cashed_out = True
        self.cashout_multiplier = current
        self.is_finished = True
        return current


@dataclass
class SlotsGame:
    """Игра Слоты (Игровой автомат)."""

    symbols: List[str] = field(init=False)
    reels: List[str] = field(default_factory=list)
    is_finished: bool = False
    bet: int = 0
    winnings: int = 0
    multiplier: float = 0

    # Таблица выплат
    PAYOUTS: Dict[str, float] = field(init=False)

    def __post_init__(self):
        """Инициализирует символы для слотов с разной редкостью."""
        self.symbols = ["🍒", "🍋", "🍀", "⭐", "💎", "7️⃣"]
        self.PAYOUTS = {
            "🍒": 1.5,
            "🍋": 2,
            "🍀": 3,
            "⭐": 4,
            "💎": 5,
            "7️⃣": 10,
        }

    def spin(self) -> None:
        """Вращает барабаны и определяет результат."""
        self.reels = [random.choice(self.symbols) for _ in range(3)]
        self.is_finished = True

        # Проверяем выигрыш
        if self.reels[0] == self.reels[1] == self.reels[2]:
            symbol = self.reels[0]
            self.multiplier = self.PAYOUTS[symbol]
            self.winnings = int(self.bet * self.multiplier)
        else:
            self.multiplier = 0
            self.winnings = 0

    def get_animation_frames(self, frames: int = 6) -> List[List[str]]:
        """Генерирует кадры анимации для вращения барабанов."""
        animation = []
        
        for frame in range(frames):
            # Каждый кадр показываем случайные символы
            frame_reels = [random.choice(self.symbols) for _ in range(3)]
            animation.append(frame_reels)
        
        # Последний кадр - реальный результат
        animation.append(self.reels)
        
        return animation

    def get_animation_display(self, reels: List[str]) -> str:
        """Возвращает строковое представление барабанов для анимации."""
        return " | ".join(reels)

    def get_result_text(self) -> str:
        """Возвращает текст результата."""
        return f"{' '.join(self.reels)}"

    def get_winnings_text(self) -> str:
        """Возвращает текст выигрыша."""
        if self.multiplier >= 50:
            return f"🎉🎉🎉 СУПЕР-ДЖЕКПОТ! +{self.winnings} {CURRENCY_EMOJI} ({self.multiplier}x)"
        elif self.multiplier >= 20:
            return f"🎉🎉 ДЖЕКПОТ! +{self.winnings} {CURRENCY_EMOJI} ({self.multiplier}x)"
        elif self.winnings > 0:
            return f"✅ Выигрыш: +{self.winnings} {CURRENCY_EMOJI} ({self.multiplier}x)"
        else:
            return f"😢 Нет выигрыша. -{self.bet} {CURRENCY_EMOJI}"

    def get_payout_table(self) -> str:
        """Возвращает таблицу выплат."""
        table = []
        for symbol, multiplier in sorted(self.PAYOUTS.items(), key=lambda x: x[1]):
            if multiplier >= 50:
                table.append(f"{symbol*3} = {multiplier}x (Супер-Джекпот!)")
            elif multiplier >= 20:
                table.append(f"{symbol*3} = {multiplier}x (Джекпот!)")
            else:
                table.append(f"{symbol*3} = {multiplier}x")
        return "\n".join(table)


@dataclass
class F1RacingGame:
    """Игра Формула 1 (Formula 1 Racing)."""

    horses: List[Dict[str, Any]] = field(init=False)
    track_length: int = 20
    is_finished: bool = False
    winner: Optional[str] = None
    bets: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # user_id -> {driver_name, bet}

    def __post_init__(self):
        """Инициализирует гонщиков Формулы 1."""
        self.horses = [
            {"name": "🏎️ K.Antonelli", "position": 0, "speed": random.uniform(0.8, 1.2)},
            {"name": "🏎️ L.Hamilton", "position": 0, "speed": random.uniform(0.8, 1.2)},
            {"name": "🏎️ M.Verstappen", "position": 0, "speed": random.uniform(0.8, 1.2)},
            {"name": "🏎️ L.Norris", "position": 0, "speed": random.uniform(0.8, 1.2)},
            {"name": "🏎️ F.Alonso", "position": 0, "speed": random.uniform(0.8, 1.2)},
        ]

    def place_bet(self, user_id: int, driver_name: str, bet: int) -> bool:
        """Размещает ставку на гонщика."""
        if self.is_finished or user_id in self.bets:
            return False
        
        # Проверяем, что гонщик существует
        if not any(driver["name"] == driver_name for driver in self.horses):
            return False
        
        self.bets[user_id] = {"driver_name": driver_name, "bet": bet}
        return True

    def race_step(self) -> bool:
        """Делает один шаг гонки Формулы 1. Возвращает True если гонка закончилась."""
        if self.is_finished:
            return True

        # Двигаем каждого гонщика
        for driver in self.horses:
            # Случайное ускорение с учётом базовой скорости
            step = random.uniform(0.5, 2.0) * driver["speed"]
            driver["position"] += step

        # Проверяем, кто первым пересёк финиш
        for driver in self.horses:
            if driver["position"] >= self.track_length:
                self.is_finished = True
                self.winner = driver["name"]
                return True

        return False

    def get_race_track(self) -> str:
        """Возвращает визуальное представление трассы Формулы 1."""
        track_lines = []
        
        for driver in self.horses:
            # Вычисляем позицию гонщика на трассе
            position = min(int(driver["position"]), self.track_length - 1)
            
            # Создаём линию трассы
            track_line = "🏁" + "─" * position + driver["name"] + "─" * (self.track_length - position - 1) + "🏁"
            
            # Добавляем индикатор если это победитель
            if self.is_finished and driver["name"] == self.winner:
                track_line += " 🏆"
            
            track_lines.append(track_line)
        
        return "\n".join(track_lines)

    def get_results(self) -> Dict[int, int]:
        """Возвращает результаты ставок гонки Формулы 1 (user_id -> winnings)."""
        if not self.is_finished or not self.winner:
            return {}

        results = {}
        for user_id, bet_info in self.bets.items():
            if bet_info["driver_name"] == self.winner:
                # Выигрыш = ставка * 2 (коэффициент 2:1)
                results[user_id] = bet_info["bet"] * 2
        
        return results




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


class ShopManager:
    """Управляет покупками в магазине и заявками на кастомные роли."""

    def __init__(self, storage_path: Path, economy: EconomyManager) -> None:
        self.storage_path = storage_path
        self.economy = economy
        self._lock = asyncio.Lock()
        self._data = self._load()

    def _load(self) -> Dict:
        if not self.storage_path.exists():
            return {"role_requests": [], "avatar_changes": [], "lottery_tickets": [], "daily_boosters": {}}
        try:
            with self.storage_path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
                # Добавляем новые поля если их нет
                if "lottery_tickets" not in data:
                    data["lottery_tickets"] = []
                if "daily_boosters" not in data:
                    data["daily_boosters"] = {}
                return data
        except (json.JSONDecodeError, OSError):
            logger.warning("Не удалось прочитать файл магазина, создаю новый.")
            return {"role_requests": [], "avatar_changes": [], "lottery_tickets": [], "daily_boosters": {}}

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.storage_path.open("w", encoding="utf-8") as fp:
            json.dump(self._data, fp, ensure_ascii=False, indent=2)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def create_role_request(
        self, user_id: int, role_name: str, role_color: str
    ) -> int:
        """Создаёт заявку на кастомную роль. Возвращает ID заявки."""
        async with self._lock:
            request_id = len(self._data["role_requests"]) + 1
            request = {
                "id": request_id,
                "user_id": user_id,
                "role_name": role_name,
                "role_color": role_color,
                "status": "pending",
                "created_at": self._now().isoformat(),
            }
            self._data["role_requests"].append(request)
            self._save()
            return request_id

    async def get_pending_role_requests(self) -> List[Dict]:
        """Возвращает список ожидающих заявок на роли."""
        async with self._lock:
            return [
                r for r in self._data["role_requests"]
                if r["status"] == "pending"
            ]

    async def approve_role_request(
        self, request_id: int, admin_id: int, role_id: Optional[int] = None
    ) -> Optional[Dict]:
        """Одобряет заявку на роль."""
        async with self._lock:
            for request in self._data["role_requests"]:
                if request["id"] == request_id and request["status"] == "pending":
                    request["status"] = "approved"
                    request["admin_id"] = admin_id
                    request["role_id"] = role_id
                    request["approved_at"] = self._now().isoformat()
                    self._save()
                    return request
            return None

    async def reject_role_request(
        self, request_id: int, admin_id: int, reason: str = ""
    ) -> Optional[Dict]:
        """Отклоняет заявку на роль и возвращает деньги."""
        async with self._lock:
            for request in self._data["role_requests"]:
                if request["id"] == request_id and request["status"] == "pending":
                    request["status"] = "rejected"
                    request["admin_id"] = admin_id
                    request["reason"] = reason
                    request["rejected_at"] = self._now().isoformat()
                    # Возврат денег
                    await self.economy.change_balance(
                        request["user_id"], SHOP_ITEMS["custom_role"]["price"]
                    )
                    self._save()
                    return request
            return None

    async def create_avatar_change(self, user_id: int, avatar_url: str) -> int:
        """Создаёт запись о смене аватарки."""
        async with self._lock:
            change_id = len(self._data["avatar_changes"]) + 1
            now = self._now()
            expires_at = now.timestamp() + (48 * 3600)
            change = {
                "id": change_id,
                "user_id": user_id,
                "avatar_url": avatar_url,
                "created_at": now.isoformat(),
                "expires_at": expires_at,
                "expired": False,
            }
            self._data["avatar_changes"].append(change)
            self._save()
            return change_id

    async def get_active_avatar_changes(self) -> List[Dict]:
        """Возвращает активные смены аватарок."""
        async with self._lock:
            now = self._now().timestamp()
            active = []
            for change in self._data["avatar_changes"]:
                if not change["expired"] and change["expires_at"] > now:
                    active.append(change)
                elif not change["expired"] and change["expires_at"] <= now:
                    change["expired"] = True
            self._save()
            return active

    async def expire_avatar_change(self, change_id: int) -> bool:
        """Помечает смену аватарки как истёкшую."""
        async with self._lock:
            for change in self._data["avatar_changes"]:
                if change["id"] == change_id:
                    change["expired"] = True
                    self._save()
                    return True
            return False

    async def add_lottery_ticket(self, user_id: int) -> int:
        """Добавляет лотерейный билет пользователю."""
        async with self._lock:
            ticket = {
                "user_id": user_id,
                "purchased_at": self._now().isoformat(),
                "drawn": False,
            }
            self._data["lottery_tickets"].append(ticket)
            self._save()
            return len(self._data["lottery_tickets"])

    async def get_active_lottery_tickets(self) -> List[Dict]:
        """Возвращает неразыгранные билеты."""
        async with self._lock:
            return [t for t in self._data["lottery_tickets"] if not t.get("drawn", False)]

    async def draw_lottery_winner(self) -> Optional[Dict]:
        """Проводит розыгрыш и возвращает победителя."""
        async with self._lock:
            active = [t for t in self._data["lottery_tickets"] if not t.get("drawn", False)]
            if not active:
                return None
            winner = random.choice(active)
            winner["drawn"] = True
            winner["won_at"] = self._now().isoformat()
            # Джекпот = количество билетов * цена билета * 0.8 (80% собранного)
            jackpot = len(active) * SHOP_ITEMS["lottery_ticket"]["price"] * 8 // 10
            winner["jackpot"] = jackpot
            # Помечаем все билеты как розыгранные
            for t in active:
                t["drawn"] = True
                t["round_id"] = winner.get("user_id")
            self._save()
            return winner

    async def has_daily_booster(self, user_id: int) -> bool:
        """Проверяет, есть ли у пользователя активный удвоитель."""
        async with self._lock:
            user_key = str(user_id)
            return self._data["daily_boosters"].get(user_key, False)

    async def add_daily_booster(self, user_id: int) -> None:
        """Добавляет удвоитель бонуса пользователю."""
        async with self._lock:
            self._data["daily_boosters"][str(user_id)] = True
            self._save()

    async def use_daily_booster(self, user_id: int) -> bool:
        """Использует удвоитель (возвращает True если был активен)."""
        async with self._lock:
            user_key = str(user_id)
            if self._data["daily_boosters"].get(user_key, False):
                self._data["daily_boosters"][user_key] = False
                self._save()
                return True
            return False

    # ==================== МЕТОДЫ ДЛЯ ЭМОДЗИ ====================

    async def create_emoji_request(
        self, user_id: int, emoji_name: str, emoji_url: str
    ) -> int:
        """Создаёт заявку на добавление кастомного эмодзи. Возвращает ID заявки."""
        async with self._lock:
            # Инициализируем список заявок на эмодзи если его нет
            if "emoji_requests" not in self._data:
                self._data["emoji_requests"] = []

            request_id = len(self._data["emoji_requests"]) + 1
            request = {
                "id": request_id,
                "user_id": user_id,
                "emoji_name": emoji_name,
                "emoji_url": emoji_url,
                "status": "pending",  # pending, approved, rejected
                "created_at": self._now().isoformat(),
            }
            self._data["emoji_requests"].append(request)
            self._save()
            return request_id

    async def get_pending_emoji_requests(self) -> List[Dict]:
        """Возвращает список ожидающих заявок на эмодзи."""
        async with self._lock:
            if "emoji_requests" not in self._data:
                return []
            return [
                r for r in self._data["emoji_requests"]
                if r["status"] == "pending"
            ]

    async def approve_emoji_request(
        self, request_id: int, admin_id: int, emoji_id: Optional[str] = None
    ) -> Optional[Dict]:
        """Одобряет заявку на эмодзи."""
        async with self._lock:
            if "emoji_requests" not in self._data:
                return None

            for request in self._data["emoji_requests"]:
                if request["id"] == request_id and request["status"] == "pending":
                    request["status"] = "approved"
                    request["admin_id"] = admin_id
                    request["emoji_id"] = emoji_id
                    request["approved_at"] = self._now().isoformat()
                    self._save()
                    return request
            return None

    async def reject_emoji_request(
        self, request_id: int, admin_id: int, reason: str = ""
    ) -> Optional[Dict]:
        """Отклоняет заявку на эмодзи и возвращает деньги."""
        async with self._lock:
            if "emoji_requests" not in self._data:
                return None

            for request in self._data["emoji_requests"]:
                if request["id"] == request_id and request["status"] == "pending":
                    request["status"] = "rejected"
                    request["admin_id"] = admin_id
                    request["reason"] = reason
                    request["rejected_at"] = self._now().isoformat()
                    # Возврат денег
                    await self.economy.change_balance(
                        request["user_id"], SHOP_ITEMS["custom_emoji"]["price"]
                    )
                    self._save()
                    return request
            return None


class TotalsManager:
    """Управляет тотализатором (ставками на события сервера)."""

    def __init__(self, storage_path: Path, economy: EconomyManager) -> None:
        self.storage_path = storage_path
        self.economy = economy
        self._lock = asyncio.Lock()
        self._data = self._load()

    def _load(self) -> Dict:
        """Загружает данные тотализатора из файла."""
        if not self.storage_path.exists():
            return {"events": [], "event_counter": 0}
        try:
            with self.storage_path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
                # Добавляем новые поля если их нет
                if "event_counter" not in data:
                    data["event_counter"] = 0
                return data
        except (json.JSONDecodeError, OSError):
            logger.warning("Не удалось прочитать файл тотализатора, создаю новый.")
            return {"events": [], "event_counter": 0}

    def _save(self) -> None:
        """Сохраняет данные тотализатора в файл."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.storage_path.open("w", encoding="utf-8") as fp:
            json.dump(self._data, fp, ensure_ascii=False, indent=2)

    def _now(self) -> datetime:
        """Возвращает текущее время в UTC."""
        return datetime.now(timezone.utc)

    async def create_event(
        self,
        creator_id: int,
        title: str,
        description: str,
        options: List[str],
        min_bet: int = TOTALS_DEFAULT_MIN_BET,
        max_bet: int = TOTALS_DEFAULT_MAX_BET,
    ) -> int:
        """Создаёт новое событие для ставок. Возвращает ID события."""
        async with self._lock:
            self._data["event_counter"] += 1
            event_id = self._data["event_counter"]

            event = {
                "id": event_id,
                "title": title,
                "description": description,
                "options": options,
                "min_bet": min_bet,
                "max_bet": max_bet,
                "status": "active",  # active, closed, cancelled
                "creator_id": creator_id,
                "created_at": self._now().isoformat(),
                "closed_at": None,
                "winning_option": None,
                "bets": [],  # Список ставок
                "total_pool": 0,  # Общий пул ставок
            }
            self._data["events"].append(event)
            self._save()
            return event_id

    async def place_bet(
        self, event_id: int, user_id: int, option_index: int, amount: int
    ) -> Optional[Dict]:
        """Делает ставку на событие. Возвращает результат или None при ошибке."""
        async with self._lock:
            event = self._get_event(event_id)
            if not event or event["status"] != "active":
                return None

            # Проверяем границы ставки
            if amount < event["min_bet"] or amount > event["max_bet"]:
                return {"error": "bet_range", "min": event["min_bet"], "max": event["max_bet"]}

            # Проверяем валидность варианта
            if option_index < 0 or option_index >= len(event["options"]):
                return {"error": "invalid_option"}

            # Проверяем баланс
            balance = await self.economy.ensure_account(user_id)
            if balance < amount:
                return {"error": "insufficient_funds", "balance": balance}

            # Проверяем, не ставил ли уже пользователь на этот вариант
            existing_bet = next(
                (b for b in event["bets"] if b["user_id"] == user_id and b["option_index"] == option_index),
                None
            )
            if existing_bet:
                return {"error": "already_bet"}

            # Списываем деньги
            await self.economy.change_balance(user_id, -amount)

            # Добавляем ставку
            bet = {
                "user_id": user_id,
                "option_index": option_index,
                "amount": amount,
                "placed_at": self._now().isoformat(),
            }
            event["bets"].append(bet)
            event["total_pool"] += amount
            self._save()

            return {"success": True, "bet": bet, "event": event}

    async def close_event(self, event_id: int, winning_option_index: int) -> Optional[Dict]:
        """Закрывает событие и определяет победителей."""
        async with self._lock:
            event = self._get_event(event_id)
            if not event or event["status"] != "active":
                return None

            # Проверяем валидность выигрышного варианта
            if winning_option_index < 0 or winning_option_index >= len(event["options"]):
                return None

            event["status"] = "closed"
            event["winning_option"] = winning_option_index
            event["closed_at"] = self._now().isoformat()

            # Находим победителей
            winning_bets = [b for b in event["bets"] if b["option_index"] == winning_option_index]
            total_winning_bets = sum(b["amount"] for b in winning_bets)

            # Распределяем выигрыши
            winners = []
            if winning_bets:
                # Коэффициент: пул / сумма выигрышных ставок (комиссия 10%)
                payout_pool = int(event["total_pool"] * 0.9)
                for bet in winning_bets:
                    # Пропорциональный выигрыш
                    win_amount = int(payout_pool * (bet["amount"] / total_winning_bets))
                    await self.economy.change_balance(bet["user_id"], win_amount)
                    winners.append({
                        "user_id": bet["user_id"],
                        "bet": bet["amount"],
                        "win": win_amount,
                    })

            self._save()
            return {"event": event, "winners": winners, "total_winning_bets": total_winning_bets}

    async def cancel_event(self, event_id: int) -> Optional[Dict]:
        """Отменяет событие и возвращает все ставки."""
        async with self._lock:
            event = self._get_event(event_id)
            if not event or event["status"] != "active":
                return None

            event["status"] = "cancelled"
            event["closed_at"] = self._now().isoformat()

            # Возвращаем все ставки
            for bet in event["bets"]:
                await self.economy.change_balance(bet["user_id"], bet["amount"])

            self._save()
            return {"event": event, "refunded_count": len(event["bets"])}

    def _get_event(self, event_id: int) -> Optional[Dict]:
        """Возвращает событие по ID."""
        for event in self._data["events"]:
            if event["id"] == event_id:
                return event
        return None

    async def get_active_events(self) -> List[Dict]:
        """Возвращает список активных событий."""
        async with self._lock:
            return [e for e in self._data["events"] if e["status"] == "active"]

    async def get_event_status(self, event_id: int) -> Optional[Dict]:
        """Возвращает статус события со статистикой ставок."""
        async with self._lock:
            event = self._get_event(event_id)
            if not event:
                return None

            # Считаем статистику по вариантам
            stats = {}
            for i, option in enumerate(event["options"]):
                option_bets = [b for b in event["bets"] if b["option_index"] == i]
                stats[i] = {
                    "option": option,
                    "total_bets": len(option_bets),
                    "total_amount": sum(b["amount"] for b in option_bets),
                    "participants": [b["user_id"] for b in option_bets],
                }

            return {"event": event, "stats": stats}

    async def get_user_bets(self, user_id: int) -> List[Dict]:
        """Возвращает все ставки пользователя."""
        async with self._lock:
            user_bets = []
            for event in self._data["events"]:
                for bet in event["bets"]:
                    if bet["user_id"] == user_id:
                        user_bets.append({
                            "event_id": event["id"],
                            "event_title": event["title"],
                            "event_status": event["status"],
                            "option": event["options"][bet["option_index"]],
                            "amount": bet["amount"],
                            "winning_option": event.get("winning_option"),
                        })
            return user_bets


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
            message = f"🎉 Вы выиграли {payout} рублей. Баланс: {new_balance}."
        elif payout < 0:
            message = f"😢 Вы проиграли {abs(payout)} рублей. Баланс: {new_balance}."
        else:
            message = f"🤝 Ничья. Баланс без изменений: {new_balance}."

        await interaction.followup.send(message, ephemeral=True)

    def _calculate_payout(self) -> int:
        if "победили" in self.game.result and "проиграли" not in self.game.result:
            return self.bet
        if "проиграли" in self.game.result or "Перебор" in self.game.result:
            return -self.bet
        return 0


class CrashView(discord.ui.View):
    """Интерактивный вид для игры Crash с обновляющимся множителем."""

    REFRESH_RATE: float = 0.5  # Обновление каждые 0.5 сек

    def __init__(
        self,
        player_id: int,
        player_name: str,
        game: CrashGame,
        bet: int,
        economy_manager: EconomyManager,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.player_id = player_id
        self.player_name = player_name
        self.game = game
        self.bet = bet
        self.economy = economy_manager
        self._finished = False
        self._payout_applied = False
        self._update_task: Optional[asyncio.Task] = None

    async def start_updates(self, interaction: discord.Interaction) -> None:
        """Запускает цикл обновления множителя."""
        self._update_task = asyncio.create_task(self._update_loop(interaction))

    async def _update_loop(self, interaction: discord.Interaction) -> None:
        """Цикл обновления сообщения с множителем."""
        try:
            while not self.game.is_finished and not self.is_finished():
                await asyncio.sleep(self.REFRESH_RATE)

                # Проверяем краш
                if self.game.check_crash():
                    await self._finish_game(interaction, crashed=True)
                    return

                # Обновляем сообщение
                if not self._finished:
                    await self._refresh_message(interaction)

        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.exception("Ошибка в цикле обновления Crash: %s", error)

    def is_finished(self) -> bool:
        """Проверяет, завершено ли взаимодействие."""
        return self._finished

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        """Обновляет embed с текущим множителем."""
        multiplier = self.game.get_current_multiplier()
        embed = self._build_embed(multiplier, crashed=False)

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            pass
        except Exception as error:
            logger.debug("Не удалось обновить сообщение Crash: %s", error)

    def _build_embed(self, multiplier: float, crashed: bool = False) -> discord.Embed:
        """Создаёт embed для текущего состояния игры."""
        if crashed:
            embed = discord.Embed(
                title="💥 КРАШ!",
                description=f"Множитель остановился на **{multiplier:.2f}x**",
                color=discord.Color.red(),
            )
            embed.add_field(name="Результат", value=f"😢 Вы проиграли **{self.bet}** {CURRENCY_EMOJI}", inline=False)
        elif self.game.cashed_out:
            winnings = int(self.bet * self.game.cashout_multiplier)
            profit = winnings - self.bet
            embed = discord.Embed(
                title="✅ Забрано!",
                description=f"Вы забрали на **{multiplier:.2f}x**",
                color=discord.Color.green(),
            )
            embed.add_field(name="Выигрыш", value=f"**{winnings}** {CURRENCY_EMOJI}", inline=True)
            embed.add_field(name="Прибыль", value=f"**+{profit}** {CURRENCY_EMOJI}", inline=True)
        else:
            embed = discord.Embed(
                title="🚀 Краш",
                description=f"Множитель растёт: **{multiplier:.2f}x**",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Ставка", value=f"**{self.bet}** {CURRENCY_EMOJI}", inline=True)
            embed.add_field(name="Потенциальный выигрыш", value=f"**{int(self.bet * multiplier)}** {CURRENCY_EMOJI}", inline=True)
            embed.set_footer(text="Нажмите «Забрать» чтобы забрать выигрыш!")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверяет, что нажимает только владелец игры."""
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "Эта игра принадлежит другому пользователю.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        """Автоматически забирает при таймауте."""
        if not self.game.is_finished and not self._finished:
            self._finished = True
            if self._update_task:
                self._update_task.cancel()

    @discord.ui.button(label="Забрать", style=discord.ButtonStyle.success)
    async def cashout_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Обрабатывает нажатие кнопки «Забрать»."""
        try:
            if self.game.is_finished or self._finished:
                return

            # Проверяем, не крашнулось ли
            if self.game.check_crash():
                await self._finish_game(interaction, crashed=True)
                return

            # Забираем
            multiplier = self.game.cash_out()
            if multiplier is not None and multiplier > 0:
                await self._finish_game(interaction, crashed=False)
            else:
                await self._finish_game(interaction, crashed=True)

        except Exception as error:
            logger.exception("Ошибка при выводе в Crash: %s", error)

    async def _finish_game(self, interaction: discord.Interaction, crashed: bool) -> None:
        """Завершает игру и начисляет выигрыш."""
        if self._finished:
            return
        self._finished = True

        # Отменяем цикл обновления
        if self._update_task:
            self._update_task.cancel()

        # Отключаем кнопки
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # Начисляем выигрыш
        if not crashed and self.game.cashed_out:
            winnings = int(self.bet * self.game.cashout_multiplier)
            await self.economy.change_balance(self.player_id, winnings)
            new_balance = await self.economy.ensure_account(self.player_id)

            embed = self._build_embed(self.game.cashout_multiplier, crashed=False)
            embed.add_field(name="Баланс", value=f"**{new_balance}** {CURRENCY_EMOJI}", inline=False)
        else:
            embed = self._build_embed(self.game.crash_point, crashed=True)

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            pass


class HelpView(discord.ui.View):
    """Интерактивный вид для команды помощи с выбором разделов."""

    def __init__(self, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)

    @discord.ui.button(label="🎮 Игры", style=discord.ButtonStyle.primary)
    async def games_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Показывает информацию об играх."""
        embed = discord.Embed(
            title="🎮 Игры",
            description="Доступные мини-игры в боте:",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="🃏 Блэкджек",
            value="`/blackjack` — классическая карточная игра на 21 очко\n"
                  "Цель: набрать 21 очко, но не перебрать\n"
                  "Кнопки: 📥 Взять карту, 🛑 Стоп",
            inline=False
        )
        
        embed.add_field(
            name="📈 Crash",
            value="`/crash` — игра с растущим множителем\n"
                  "Цель: успеть забрать деньги до краша\n"
                  "Чем дольше ждёте, тем выше множитель",
            inline=False
        )
        
        embed.add_field(
            name="🎰 Слоты",
            value="`/slots` — игровой автомат с тремя барабанами\n"
                  "Таблица выплат:\n"
                  "🍒🍒🍒 = 1.5x | 🍋🍋🍋 = 2x | 🍀🍀🍀 = 3x\n"
                  "⭐⭐⭐ = 4x | 💎💎💎 = 5x (Джекпот!) | 7️⃣7️⃣7️⃣ = 10x (Супер-Джекпот!)",
            inline=False
        )
        
        embed.add_field(
            name="🏎️ Формула 1",
            value="`/formula1` — делайте ставки на гонщиков и наблюдайте за гонку\n"
                  "Коэффициент выигрыша: 2:1 | Максимальная ставка: 10,000 рублей\n"
                  "Доступные гонщики: 🏎️ K.Antonelli, 🏎️ L.Hamilton, 🏎️ M.Verstappen, 🏎️ L.Norris, 🏎️ F.Alonso\n"
                  "Гонка происходит в реальном времени с анимацией!",
            inline=False
        )
        
        embed.add_field(
            name="📊 Тотализатор",
            value="`/totals_list` — список активных событий для ставок\n"
                  "`/totals_bet` — сделать ставку на исход события\n"
                  "`/totals_status` — статус события и текущие ставки\n"
                  "Делайте ставки на события сервера: кто победит, кого забанят и т.д.!",
            inline=False
        )
        
        embed.set_footer(text="Максимальная ставка в играх: 10,000 рублей")
        
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="💰 Экономика", style=discord.ButtonStyle.success)
    async def economy_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Показывает информацию об экономике."""
        embed = discord.Embed(
            title="💰 Экономика",
            description="Управление деньгами и магазин:",
            color=discord.Color.gold()
        )
        
        embed.add_field(
            name="💳 Баланс",
            value="`/balance` — показать текущий баланс\n"
                  "`/leaderboard` — топ игроков по деньгам\n"
                  "`/pay` — перевести деньги другому игроку",
            inline=False
        )
        
        embed.add_field(
            name="🎁 Бонусы",
            value="`/daily` — ежедневный бонус (1000 рублей)\n"
                  "В магазине можно купить удвоитель ежедневного бонуса",
            inline=False
        )
        
        embed.add_field(
            name="🎰 Лотерея",
            value="`/lottery_status` — статус лотереи и количество билетов\n"
                  "Купите билет в магазине и ждите розыгрыша!",
            inline=False
        )
        
        embed.add_field(
            name="💬 Фарм за сообщения",
            value="`/farm_status` — проверить прогресс до следующей награды\n"
                  f"За каждые 30 сообщений в специальном канале — 5 {CURRENCY_EMOJI}!",
            inline=False
        )
        
        embed.add_field(
            name="🛍️ Магазин",
            value="`/shop` — показать товары\n"
                  "`/buy` — купить товар:\n"
                  "• `custom_role` — кастомная роль\n"
                  "• `custom_emoji` — кастомный эмодзи-слот\n"
                  "• `nickname_change` — 📝 смена ника другу\n"
                  "• `joke_mute` — 🔇 мут друга на 5 минут\n"
                  "• `server_avatar_2d` — смена аватарки сервера\n"
                  "• `lottery_ticket` — лотерейный билет\n"
                  "• `daily_booster` — удвоитель ежедневного бонуса\n"
                  "• `999_rubles` — 999 рублей",
            inline=False
        )
        
        embed.add_field(
            name="💸 Донат",
            value="`/donate` — способы поддержать проект",
            inline=False
        )
        
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🛠️ Админские команды", style=discord.ButtonStyle.secondary)
    async def admin_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Показывает информацию об админских командах."""
        embed = discord.Embed(
            title="🛠️ Админские команды",
            description="Команды доступны только администраторам:",
            color=discord.Color.red()
        )
        
        embed.add_field(
            name="💵 Управление деньгами",
            value="`/grant` — начислить деньги себе",
            inline=False
        )
        
        embed.add_field(
            name="🎨 Управление ролями",
            value="`/role_requests` — список заявок на кастомные роли\n"
                  "`/approve_role` — одобрить заявку на роль\n"
                  "`/reject_role` — отклонить заявку на роль",
            inline=False
        )
        
        embed.add_field(
            name="🖼️ Управление аватарками",
            value="`/active_avatars` — активные смены аватарок\n"
                  "`/draw_lottery` — провести розыгрыш лотереи",
            inline=False
        )
        
        embed.add_field(
            name="😀 Управление эмодзи",
            value="`/emoji_requests` — список заявок на кастомные эмодзи\n"
                  "`/approve_emoji` — одобрить заявку на эмодзи\n"
                  "`/reject_emoji` — отклонить заявку на эмодзи",
            inline=False
        )
        
        embed.add_field(
            name="📊 Управление тотализатором",
            value="`/totals_create` — создать событие для ставок\n"
                  "`/totals_close` — закрыть событие и выплатить выигрыши",
            inline=False
        )
        
        embed.add_field(
            name="💬 Управление фармом",
            value="`/set_farm_channel` — установить канал для фарма\n"
                  "`/reset_farm_counters` — сбросить все счётчики сообщений",
            inline=False
        )
        
        embed.set_footer(text="Для доступа к командам добавьте свой ID в config.json")
        
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔙 Назад", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Возвращает в главное меню."""
        embed = discord.Embed(
            title="📚 Помощь - Discord Card Games Bot",
            description="Выберите интересующий вас раздел:",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="🎮 Игры",
            value="Информация об играх: Блэкджек, Crash, Слоты, Формула 1, Тотализатор",
            inline=False
        )
        
        embed.add_field(
            name="💰 Экономика",
            value="Баланс, переводы, магазин, лотерея, фарм, ежедневный бонус",
            inline=False
        )
        
        embed.add_field(
            name="🛠️ Админские команды",
            value="Управление ролями, эмодзи, аватарками, лотереей, тотализатором, фармом",
            inline=False
        )
        
        await interaction.response.edit_message(embed=embed, view=self)




class F1RacingView(discord.ui.View):
    """Интерактивный вид для игры Формула 1."""

    UPDATE_INTERVAL: float = 2.0  # Обновление каждые 2 секунды

    def __init__(
        self,
        game: F1RacingGame,
        economy_manager: EconomyManager,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.game = game
        self.economy = economy_manager
        self._finished = False
        self._update_task: Optional[asyncio.Task] = None

    async def start_race(self, interaction: discord.Interaction) -> None:
        """Начинает гонку Формулы 1 и запускает обновления."""
        self._update_task = asyncio.create_task(self._race_loop(interaction))

    async def _race_loop(self, interaction: discord.Interaction) -> None:
        """Основной цикл гонки Формулы 1 с обновлениями."""
        try:
            while not self.game.is_finished and not self._finished:
                # Делаем шаг гонки Формулы 1
                self.game.race_step()
                
                # Обновляем сообщение
                await self._update_message(interaction)
                
                # Пауза между шагами
                await asyncio.sleep(self.UPDATE_INTERVAL)

            # Финальное обновление
            await self._finish_race(interaction)

        except Exception as error:
            logger.exception("Ошибка в цикле гонки Формулы 1: %s", error)

    async def _update_message(self, interaction: discord.Interaction) -> None:
        """Обновляет сообщение с текущим состоянием гонки Формулы 1."""
        embed = self._build_embed()
        
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            self._finished = True
        except Exception as error:
            logger.debug("Не удалось обновить сообщение гонки Формулы 1: %s", error)

    def _build_embed(self) -> discord.Embed:
        """Создаёт embed для текущего состояния гонки Формулы 1."""
        if self.game.is_finished:
            embed = discord.Embed(
                title="🏁 Гонка Формулы 1 завершена!",
                description=f"**Победитель: {self.game.winner}** 🏆",
                color=discord.Color.gold(),
            )
        else:
            embed = discord.Embed(
                title="🐎 Скачки в процессе!",
                description="Лошади мчатся к финишу!",
                color=discord.Color.blue(),
            )

        # Добавляем визуальную трассу
        embed.add_field(
            name="🏁 Трасса 🏁",
            value=f"```\n{self.game.get_race_track()}\n```",
            inline=False,
        )

        # Добавляем информацию о ставках
        if self.game.bets:
            bets_text = []
            for user_id, bet_info in self.game.bets.items():
                bets_text.append(f"<@{user_id}>: {bet_info['bet']} {CURRENCY_EMOJI} на {bet_info['driver_name']}")
            
            embed.add_field(
                name="💰 Ставки",
                value="\n".join(bets_text),
                inline=False,
            )

        if not self.game.is_finished:
            embed.set_footer(text="Коэффициент выигрыша: 2:1")
        else:
            # Показываем результаты
            results = self.game.get_results()
            if results:
                winners_text = []
                for user_id, winnings in results.items():
                    winners_text.append(f"<@{user_id}>: +{winnings} {CURRENCY_EMOJI}")
                
                embed.add_field(
                    name="🎉 Выигрыши",
                    value="\n".join(winners_text),
                    inline=False,
                )

        return embed

    async def _finish_race(self, interaction: discord.Interaction) -> None:
        """Завершает гонку Формулы 1 и обрабатывает выигрыши."""
        if self._finished:
            return
        self._finished = True

        # Получаем результаты и начисляем выигрыши
        results = self.game.get_results()
        for user_id, winnings in results.items():
            await self.economy.change_balance(user_id, winnings)

        # Финальное обновление
        await self._update_message(interaction)

    async def on_timeout(self) -> None:
        """Обрабатывает таймаут."""
        self._finished = True
        if self._update_task:
            self._update_task.cancel()


class SlotsView(discord.ui.View):
    """Интерактивный вид для игры Слоты."""

    def __init__(
        self,
        player_id: int,
        player_name: str,
        game: SlotsGame,
        bet: int,
        economy_manager: EconomyManager,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.player_id = player_id
        self.player_name = player_name
        self.game = game
        self.bet = bet
        self.economy = economy_manager
        self._finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверяет, что нажимает только владелец игры."""
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "Эта игра принадлежит другому пользователю.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🎰 Вращать", style=discord.ButtonStyle.primary)
    async def spin_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Обрабатывает нажатие кнопки «Вращать»."""
        try:
            if self.game.is_finished or self._finished:
                return

            # Отключаем кнопку во время вращения
            self.children[0].disabled = True

            # Запускаем анимацию вращения
            await self._play_spin_animation(interaction)

        except Exception as error:
            logger.exception("Ошибка в Слотах: %s", error)

    async def _play_spin_animation(self, interaction: discord.Interaction) -> None:
        """Проигрывает анимацию вращения барабанов."""
        try:
            # Генерируем результат заранее
            self.game.spin()
            animation_frames = self.game.get_animation_frames(frames=8)
            
            # Проигрываем анимацию
            for i, frame in enumerate(animation_frames[:-1]):  # Все кадры кроме последнего
                embed = discord.Embed(
                    title="🎰 Слоты",
                    description="🎰 Вращение... 🎰",
                    color=discord.Color.blue(),
                )
                
                embed.add_field(
                    name="🎰 Барабаны",
                    value=f"**{self.game.get_animation_display(frame)}**",
                    inline=False,
                )
                
                embed.add_field(name="Ставка", value=f"**{self.bet}** {CURRENCY_EMOJI}", inline=True)
                embed.add_field(name="Статус", value="🔄 Вращение...", inline=True)
                
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=embed, view=self)
                else:
                    await interaction.edit_original_response(embed=embed, view=self)
                
                # Задержка между кадрами анимации
                await asyncio.sleep(0.3)
            
            # Показываем финальный результат
            await self._finish_game(interaction)
            
        except Exception as error:
            logger.exception("Ошибка в анимации слотов: %s", error)
            # Если анимация не удалась, просто показываем результат
            await self._finish_game(interaction)

    async def _finish_game(self, interaction: discord.Interaction) -> None:
        """Завершает игру и начисляет выигрыш."""
        if self._finished:
            return
        self._finished = True

        # Отключаем кнопки
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # Начисляем выигрыш или списываем ставку
        if self.game.winnings > 0:
            await self.economy.change_balance(self.player_id, self.game.winnings)
        else:
            await self.economy.change_balance(self.player_id, -self.bet)

        new_balance = await self.economy.ensure_account(self.player_id)

        # Определяем цвет в зависимости от множителя
        if self.game.multiplier >= 20:
            color = discord.Color.gold()  # Золотой для джекпота 20x+
        elif self.game.winnings > 0:
            color = discord.Color.green()  # Зелёный для обычных выигрышей
        else:
            color = discord.Color.dark_grey()  # Серый для проигрыша

        # Создаём embed с результатом
        embed = discord.Embed(
            title="🎰 Слоты",
            description=f"Результат: **{self.game.get_result_text()}**",
            color=color,
        )
        embed.add_field(name="Ставка", value=f"**{self.bet}** {CURRENCY_EMOJI}", inline=True)
        embed.add_field(name="Результат", value=self.game.get_winnings_text(), inline=True)
        embed.add_field(name="Баланс", value=f"**{new_balance}** {CURRENCY_EMOJI}", inline=False)
        embed.add_field(name="Таблица выплат", value=f"```\n{self.game.get_payout_table()}\n```", inline=False)

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            pass


class BlackjackBot(discord.Client):
    """Discord client с мини-играми и экономикой."""

    def __init__(self) -> None:
        """Configures client with minimal required intents and command tree."""
        intents = discord.Intents.default()
        intents.message_content = True  # Для чтения сообщений (реакции)
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.economy = EconomyManager(BALANCES_FILE)
        self.shop = ShopManager(SHOP_FILE, self.economy)
        self.totals = TotalsManager(TOTALS_FILE, self.economy)
        # Загружаем admin_ids из config.json
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
                self.admin_ids = set(int(x) for x in config.get("admin_ids", []))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            self.admin_ids = set()

        # === НАСТРОЙКИ ФАРМА ЗА СООБЩЕНИЯ ===
        # Загружаем настройки фарма
        self.farm_channel_id: Optional[int] = None  # ID канала для фарма
        self.message_counters: Dict[int, int] = defaultdict(int)  # user_id -> количество сообщений
        self._load_farm_settings()

        # === НАСТРОЙКИ АВТОМАТИЧЕСКИХ РЕАКЦИЙ ===
        # Словарь триггеров: ключевое слово -> список эмодзи (выбирается случайный)
        self.reaction_triggers: Dict[str, List[str]] = {
            # Положительные эмоции
            "спасибо": ["🙏", "❤️", "👍"],
            "благодар": ["🙏", "❤️"],
            "круто": ["🔥", "😎", "👌"],
            "класс": ["👍", "🔥", "✨"],
            "супер": ["🌟", "🎉", "👏"],
            "отлично": ["💯", "🎯", "👏"],
            "ура": ["🎉", "🥳", "🎊"],
            "хаха": ["😂", "🤣", "😄"],
            "смешно": ["😂", "🤭"],
            "хихи": ["😊", "🤭", "😁"],
            # Матерные синонимы (те же реакции)
            "бля": ["😂", "🤣", "😱"],  # как хаха - удивление/смех
            "блять": ["😂", "🤣", "😱"],
            "хуй": ["😂", "🤷", "🤦"],  # как хаха - абсурд/смех
            "хуя": ["😂", "🤷", "🤦"],
            "пиздец": ["😱", "🔥", "💀"],  # как ура/победа - интенсивная эмоция
            "пизда": ["😱", "🔥", "💀"],
            "ебать": ["🤯", "🔥", "😱"],  # как круто - восхищение
            "еба": ["🤯", "🔥", "😱"],
            "нахуй": ["😤", "😠", "🤬"],  # как проиграл - раздражение
            "нах": ["😤", "😠", "🤬"],
            "сука": ["😤", "💢", "😠"],  # негатив
            "блядь": ["😤", "💢", "😠"],
            "хуёво": ["😢", "😞", "💩"],  # как проиграл - плохо
            "хуёв": ["😢", "😞", "💩"],
            "пиздато": ["🔥", "😎", "🤩"],  # как круто - круто
            "охуенно": ["🔥", "😎", "🤩"],
            "охуительно": ["🔥", "😎", "🤩"],
            "заебись": ["🔥", "💯", "😎"],  # как отлично
            "збс": ["🔥", "💯", "😎"],
            # Согласие
            "да": ["👍", "✅"],
            "ок": ["👌", "🆗", "✅"],
            "соглас": ["👍", "✅", "🤝"],
            "точно": ["💯", "👍", "🎯"],
            # Игры и выигрыши
            "победа": ["🏆", "🥇", "🎉"],
            "выиграл": ["🎰", "💰", "🎉"],
            "джекпот": ["💎", "🎰", "💰"],
            "фарт": ["🍀", "🎰", "✨"],
            "проиграл": ["💸", "😭", "🎰"],
            # Вопросы
            "?": ["🤔", "❓"],
            "вопрос": ["❓", "🤔"],
            "почему": ["🤔", "❓"],
            "как": ["🤔", "💭"],
            "что": ["🤨", "❓"],
            # Приветствие/прощание
            "привет": ["👋", "🙂"],
            "пока": ["👋", "😊"],
            "доброе утро": ["☀️", "🌅", "☕"],
            "спокойной ночи": ["🌙", "😴", "✨"],
            # Прочее
            "люблю": ["❤️", "😍", "💕"],
            "огонь": ["🔥", "🚀"],
            "го": ["🚀", "⚡"],
        }
        # Кулдаун между реакциями на одного пользователя (в секундах)
        self.reaction_cooldown: int = 5
        self._last_reaction_time: Dict[int, float] = {}
        # Шанс поставить реакцию (0.0 - 1.0, где 1.0 = 100%)
        self.reaction_chance: float = 0.3  # 30% шанс на реакцию

    async def setup_hook(self) -> None:
        """Registers and syncs application commands on startup."""
        self._register_commands()
        await self.tree.sync()
        logger.info("Команды приложения синхронизированы.")

    async def on_message(self, message: discord.Message) -> None:
        """Обрабатывает сообщения: фарм за сообщения и автоматические реакции."""
        # Игнорируем сообщения от ботов и свои собственные
        if message.author.bot:
            return

        # === ФАРМ ЗА СООБЩЕНИЯ ===
        # Проверяем, настроен ли канал для фарма и совпадает ли с текущим
        if self.farm_channel_id and message.channel.id == self.farm_channel_id:
            user_id = message.author.id
            self.message_counters[user_id] += 1

            # Проверяем, достигнуто ли количество для награды
            if self.message_counters[user_id] >= FARM_MESSAGES_REQUIRED:
                # Сбрасываем счётчик и начисляем награду
                self.message_counters[user_id] = 0
                new_balance = await self.economy.change_balance(user_id, FARM_REWARD)

                # Отправляем уведомление (ephemeral через followup невозможен, поэтому просто реакция или личное сообщение)
                try:
                    embed = discord.Embed(
                        title="💰 Награда за активность!",
                        description=f"Вы получили **{FARM_REWARD} {CURRENCY_EMOJI}** за {FARM_MESSAGES_REQUIRED} сообщений!",
                        color=discord.Color.green(),
                    )
                    embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=False)
                    embed.set_footer(text="Продолжайте общаться! 🎉")
                    await message.reply(embed=embed, delete_after=10)
                except discord.Forbidden:
                    pass  # Нет прав на ответ

                # Сохраняем обновлённые счётчики
                self._save_farm_settings()

        # === АВТОМАТИЧЕСКИЕ РЕАКЦИИ ===
        # Проверяем кулдаун для этого пользователя
        current_time = asyncio.get_event_loop().time()
        last_time = self._last_reaction_time.get(message.author.id, 0)

        if current_time - last_time < self.reaction_cooldown:
            return

        # Проверяем шанс на реакцию (вероятность)
        if random.random() > self.reaction_chance:
            return  # Не ставим реакцию этот раз

        # Проверяем сообщение на наличие триггеров
        text_lower = message.content.lower()
        for trigger, emojis in self.reaction_triggers.items():
            if trigger in text_lower:
                # Выбираем случайный эмодзи из списка
                reaction = random.choice(emojis)
                try:
                    await message.add_reaction(reaction)
                    self._last_reaction_time[message.author.id] = current_time
                    break  # Ставим только одну реакцию на сообщение
                except discord.Forbidden:
                    pass  # Нет прав на добавление реакций
                except discord.HTTPException:
                    pass  # Ошибка при добавлении реакции

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
                        f"Недостаточно денег. Ваш баланс: {balance}.",
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
                        f"Недостаточно денег. Ваш баланс: {balance}.",
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
                        payout = bet * 15
                elif choice == spin_color:
                    is_win = True
                    if choice == "зелёное":
                        payout = bet * 25
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

        @self.tree.command(name="crash", description="🚀 Игра Crash — забери выигрыш до краша!")
        @app_commands.describe(bet="Размер ставки")
        async def crash(
            interaction: discord.Interaction,
            bet: app_commands.Range[int, 1, 500],
        ) -> None:
            """Запускает игру Crash с растущим множителем."""
            try:
                balance = await self.economy.ensure_account(interaction.user.id)
                if balance < bet:
                    await interaction.response.send_message(
                        f"Недостаточно денег. Ваш баланс: {balance}.",
                        ephemeral=True,
                    )
                    return

                # Списываем ставку
                await self.economy.change_balance(interaction.user.id, -bet)

                # Создаём игру
                game = CrashGame()
                view = CrashView(
                    player_id=interaction.user.id,
                    player_name=interaction.user.display_name,
                    game=game,
                    bet=bet,
                    economy_manager=self.economy,
                )

                # Начальное сообщение
                embed = discord.Embed(
                    title="🚀 Краш",
                    description="Множитель растёт: **1.00x**",
                    color=discord.Color.gold(),
                )
                embed.add_field(name="Ставка", value=f"**{bet}** {CURRENCY_EMOJI}", inline=True)
                embed.add_field(name="Потенциальный выигрыш", value=f"**{bet}** {CURRENCY_EMOJI}", inline=True)
                embed.set_footer(text="Нажмите «Забрать» чтобы забрать выигрыш!")

                await interaction.response.send_message(embed=embed, view=view)

                # Запускаем цикл обновления
                await view.start_updates(interaction)

            except Exception as error:
                logger.exception("Не удалось запустить игру Crash: %s", error)
                if interaction.response.is_done():
                    await interaction.followup.send("Не удалось запустить Crash.", ephemeral=True)
                else:
                    await interaction.response.send_message("Не удалось запустить Crash.", ephemeral=True)

        @self.tree.command(name="slots", description="🎰 Слоты — вращай барабаны и выигрывай!")
        @app_commands.describe(bet="Размер ставки (макс. 10,000)")
        async def slots(
            interaction: discord.Interaction,
            bet: app_commands.Range[int, 1, 10_000] = 500,
        ) -> None:
            """Запускает игру Слоты."""
            try:
                balance = await self.economy.ensure_account(interaction.user.id)
                if balance < bet:
                    await interaction.response.send_message(
                        f"Недостаточно денег. Ваш баланс: {balance}.",
                        ephemeral=True,
                    )
                    return

                # Создаём игру
                game = SlotsGame()
                game.bet = bet

                view = SlotsView(
                    player_id=interaction.user.id,
                    player_name=interaction.user.display_name,
                    game=game,
                    bet=bet,
                    economy_manager=self.economy,
                )

                # Начальное сообщение
                embed = discord.Embed(
                    title="🎰 Слоты",
                    description="Нажмите «Вращать» чтобы запустить барабаны!",
                    color=discord.Color.gold(),
                )
                embed.add_field(name="Ставка", value=f"**{bet}** {CURRENCY_EMOJI}", inline=True)
                embed.add_field(name="Макс. выигрыш", value=f"**{bet * 10}** {CURRENCY_EMOJI}", inline=True)
                embed.set_footer(text="7️⃣7️⃣7️⃣ = Супер-Джекпот (10x)!")

                await interaction.response.send_message(embed=embed, view=view)

            except Exception as error:
                logger.exception("Не удалось запустить Слоты: %s", error)
                if interaction.response.is_done():
                    await interaction.followup.send("Не удалось запустить Слоты.", ephemeral=True)
                else:
                    await interaction.response.send_message("Не удалось запустить Слоты.", ephemeral=True)

        @self.tree.command(name="formula1", description="🏎️ Формула 1 — делайте ставки на гонщиков и наблюдайте за гонкой!")
        @app_commands.describe(driver="Гонщик для ставки", bet="Размер ставки")
        @app_commands.choices(
            driver=[
                app_commands.Choice(name="K.Antonelli", value="🏎️ K.Antonelli"),
                app_commands.Choice(name="L.Hamilton", value="🏎️ L.Hamilton"),
                app_commands.Choice(name="M.Verstappen", value="🏎️ M.Verstappen"),
                app_commands.Choice(name="L.Norris", value="🏎️ L.Norris"),
                app_commands.Choice(name="F.Alonso", value="🏎️ F.Alonso"),
            ]
        )
        async def formula1(
            interaction: discord.Interaction,
            driver: str = "🏎️ K.Antonelli",
            bet: app_commands.Range[int, 1, 10_000] = 500,
        ) -> None:
            """Запускает игру Формула 1."""
            try:
                balance = await self.economy.ensure_account(interaction.user.id)
                if balance < bet:
                    await interaction.response.send_message(
                        f"Недостаточно денег. Ваш баланс: {balance}.",
                        ephemeral=True,
                    )
                    return

                # Создаём игру
                game = F1RacingGame()
                
                # Размещаем ставку
                if not game.place_bet(interaction.user.id, driver, bet):
                    await interaction.response.send_message(
                        "Не удалось разместить ставку. Проверьте название гонщика.",
                        ephemeral=True,
                    )
                    return

                # Списываем ставку
                await self.economy.change_balance(interaction.user.id, -bet)

                view = F1RacingView(
                    game=game,
                    economy_manager=self.economy,
                )

                # Начальное сообщение
                embed = discord.Embed(
                    title="🏎️ Формула 1!",
                    description=f"Ставка <@{interaction.user.id}>: **{bet}** {CURRENCY_EMOJI} на {driver}",
                    color=discord.Color.red(),
                )
                
                embed.add_field(
                    name="🏁 Трасса 🏁",
                    value=f"```\n{game.get_race_track()}\n```",
                    inline=False,
                )
                
                embed.add_field(
                    name="🏎️ Доступные гонщики",
                    value="\n".join([f"• {horse['name']}" for horse in game.horses]),
                    inline=False,
                )
                
                embed.set_footer(text="Гонка начнётся через 3 секунды! Коэффициент выигрыша: 2:1")

                await interaction.response.send_message(embed=embed, view=view)
                
                # Начинаем гонку через 3 секунды
                await asyncio.sleep(3)
                await view.start_race(interaction)

            except Exception as error:
                logger.exception("Не удалось запустить Скачки: %s", error)
                if interaction.response.is_done():
                    await interaction.followup.send("Не удалось запустить Скачки.", ephemeral=True)
                else:
                    await interaction.response.send_message("Не удалось запустить Скачки.", ephemeral=True)

        
        @self.tree.command(name="help", description="📚 Помощь по командам бота")
        async def help_command(interaction: discord.Interaction) -> None:
            """Показывает главное меню помощи с выбором разделов."""
            
            embed = discord.Embed(
                title="📚 Помощь - Discord Card Games Bot",
                description="Выберите интересующий вас раздел:",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="🎮 Игры",
                value="Информация об играх: Блэкджек, Crash, Слоты, Формула 1, Тотализатор",
                inline=False
            )
            
            embed.add_field(
                name="💰 Экономика",
                value="Баланс, переводы, магазин, лотерея, фарм, ежедневный бонус",
                inline=False
            )
            
            embed.add_field(
                name="🛠️ Админские команды",
                value="Управление ролями, эмодзи, аватарками, лотереей, тотализатором, фармом",
                inline=False
            )
            
            view = HelpView()
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        @self.tree.command(name="balance", description="Показать текущий баланс деньгами")
        async def balance_command(interaction: discord.Interaction) -> None:
            amount = await self.economy.ensure_account(interaction.user.id)
            await interaction.response.send_message(
                f"Ваш баланс: **{amount}** рублей.",
                ephemeral=True,
            )

        @self.tree.command(name="leaderboard", description="Топ игроков по количеству денег")
        async def leaderboard(interaction: discord.Interaction) -> None:
            top = await self.economy.top_balances()
            if not top:
                description = "Пока нет данных."
            else:
                lines = [
                    f"{idx}. <@{user_id}> — **{amount}** рублей"
                    for idx, (user_id, amount) in enumerate(top, start=1)
                ]
                description = "\n".join(lines)

            embed = discord.Embed(
                title="🏆 Лидеры по деньгам",
                description=description,
                color=discord.Color.purple(),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="daily", description="Ежедневный бонус денег")
        async def daily(interaction: discord.Interaction) -> None:
            # Проверяем удвоитель перед начислением
            has_booster = await self.shop.has_daily_booster(interaction.user.id)
            reward = DAILY_REWARD_AMOUNT * 2 if has_booster else DAILY_REWARD_AMOUNT

            success, balance_amount, remaining = await self.economy.claim_daily(interaction.user.id)
            if success:
                # Если есть перманентный удвоитель, добавляем ещё денег
                if has_booster:
                    # Начисляем дополнительно (т.к. claim_daily уже дал базовую сумму)
                    extra = DAILY_REWARD_AMOUNT  # ещё 50
                    final_balance = await self.economy.change_balance(interaction.user.id, extra)
                    await interaction.response.send_message(
                        f"⚡ **Перманентный удвоитель активен!**\nВы получили **{reward} {CURRENCY_EMOJI}**! Баланс: {final_balance} {CURRENCY_EMOJI}.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        f"Вы получили ежедневные **{reward} {CURRENCY_EMOJI}**! Баланс: {balance_amount} {CURRENCY_EMOJI}.",
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

        @self.tree.command(name="pay", description="Перевести деньги другому пользователю")
        @app_commands.describe(user="Получатель", amount="Сумма перевода")
        async def pay(
            interaction: discord.Interaction,
            user: discord.User,
            amount: app_commands.Range[int, 1, 1_000_000],
        ) -> None:
            if user.id == interaction.user.id:
                await interaction.response.send_message(
                    "Нельзя переводить деньги самому себе.",
                    ephemeral=True,
                )
                return

            sender_balance = await self.economy.ensure_account(interaction.user.id)
            await self.economy.ensure_account(user.id)

            if sender_balance < amount:
                await interaction.response.send_message(
                    f"Недостаточно денег. Ваш баланс: {sender_balance}.",
                    ephemeral=True,
                )
                return

            new_sender_balance = await self.economy.change_balance(interaction.user.id, -amount)
            await self.economy.change_balance(user.id, amount)

            await interaction.response.send_message(
                f"Вы перевели <@{user.id}> **{amount}** рублей. Ваш баланс: {new_sender_balance}.",
                ephemeral=True,
            )

        @self.tree.command(name="grant", description="(Админ) начислить денег себе")
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
                f"Вы начислили себе {amount} рублей. Новый баланс: {new_balance}.",
                ephemeral=True,
            )

        @self.tree.command(name="donate", description="Информация о спонсорстве сервера")
        async def donate(interaction: discord.Interaction) -> None:
            embed = discord.Embed(
                title="💎 Поддержи наш проект и стань нашим Спонсором!",
                color=discord.Color.gold(),
            )

            benefits = (
                "• Роль **Спонсор**: выделись среди остальных участников\n"
                "• 20 000 валюты в боте с гэмблингом\n"
            )
            embed.add_field(
                name="Что ты получишь, став спонсором:",
                value=benefits,
                inline=False,
            )

            where_money = (
                "• Бусты для повышения уровня сервера\n"
                "• Проведение розыгрышей и различных мероприятий\n"
            )
            embed.add_field(
                name="Куда уйдут деньги:",
                value=where_money,
                inline=False,
            )

            methods = (
                "**Банковская карта** — `2204 3206 9277 9020` \n\n"
                "**DonationAlerts** — https://www.donationalerts.com/r/crstllx"
            )
            embed.add_field(
                name="Способы поддержки:",
                value=methods,
                inline=False,
            )

            embed.set_footer(
                text="Отправляйте подтверждение платежа в чат, чтобы мы знали, кто поддержал нас. "
                     "Присоединяйтесь к числу наших спонсоров и получите особые преимущества, "
                     "помогая нам расти и развиваться дальше!"
            )

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="shop", description="Открыть магазин предметов")
        async def shop_command(interaction: discord.Interaction) -> None:
            embed = discord.Embed(
                title="🏪 Магазин",
                description="Доступные товары (по возрастанию цены):",
                color=discord.Color.blue(),
            )
            # Сортируем товары по цене
            sorted_items = sorted(SHOP_ITEMS.items(), key=lambda x: x[1]["price"])
            for item_id, item in sorted_items:
                embed.add_field(
                    name=f"{item['emoji']} {item['name']} — {item['price']} {CURRENCY_EMOJI}",
                    value=f"{item['description']}\n*Купить: `/buy {item_id}`*",
                    inline=False,
                )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="buy", description="Купить предмет из магазина")
        @app_commands.describe(
            item="Что хотите купить",
            role_name="Название кастомной роли (только для custom_role)",
            role_color="Цвет роли в HEX (только для custom_role, например #FF5733)",
            avatar_url="URL аватарки (только для server_avatar_2d)",
            emoji_name="Название эмодзи (только для custom_emoji, 2-32 символа)",
            emoji_url="URL изображения эмодзи (только для custom_emoji, 128x128 PNG/JPG)",
            target_user="Пользователь для мута/смены ника (только для joke_mute и nickname_change)",
            new_nick="Новый никнейм (только для nickname_change, 1-32 символа)",
        )
        @app_commands.choices(
            item=[
                app_commands.Choice(name="Кастомная роль", value="custom_role"),
                app_commands.Choice(name="Смена аватарки (на 24 часа)", value="server_avatar_2d"),
                app_commands.Choice(name="Лотерейный билет", value="lottery_ticket"),
                app_commands.Choice(name="Удвоитель бонуса", value="daily_booster"),
                app_commands.Choice(name="999 рублей за 1000", value="999_rubles"),
                app_commands.Choice(name="Кастомный эмодзи", value="custom_emoji"),
                app_commands.Choice(name="Мут ради шутки (5 минут)", value="joke_mute"),
                app_commands.Choice(name="Смена ника другу", value="nickname_change"),
            ]
        )
        async def buy(
            interaction: discord.Interaction,
            item: str,
            role_name: Optional[str] = None,
            role_color: Optional[str] = None,
            avatar_url: Optional[str] = None,
            emoji_name: Optional[str] = None,
            emoji_url: Optional[str] = None,
            target_user: Optional[discord.Member] = None,
            new_nick: Optional[str] = None,
        ) -> None:
            if item not in SHOP_ITEMS:
                await interaction.response.send_message(
                    "Неизвестный предмет.", ephemeral=True
                )
                return

            item_data = SHOP_ITEMS[item]
            price = item_data["price"]
            balance = await self.economy.ensure_account(interaction.user.id)

            if balance < price:
                await interaction.response.send_message(
                    f"Недостаточно средств. Нужно: {price} {CURRENCY_EMOJI}, у вас: {balance} {CURRENCY_EMOJI}.",
                    ephemeral=True,
                )
                return

            if item == "custom_role":
                if not role_name or not role_color:
                    await interaction.response.send_message(
                        "Для покупки роли укажите `role_name` и `role_color` (HEX формат, например #FF5733).",
                        ephemeral=True,
                    )
                    return

                # Списываем деньги и создаём заявку
                new_balance = await self.economy.change_balance(interaction.user.id, -price)
                request_id = await self.shop.create_role_request(
                    interaction.user.id, role_name, role_color
                )

                embed = discord.Embed(
                    title="✅ Заявка на кастомную роль создана",
                    color=discord.Color.green(),
                )
                embed.add_field(name="ID заявки", value=str(request_id), inline=True)
                embed.add_field(name="Название", value=role_name, inline=True)
                embed.add_field(name="Цвет", value=role_color, inline=True)
                embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=False)
                embed.set_footer(text="Администрация рассмотрит заявку и выдаст роль.")
                await interaction.response.send_message(embed=embed, ephemeral=True)

            elif item == "server_avatar_2d":
                if not avatar_url:
                    await interaction.response.send_message(
                        "Для смены аватарки укажите `avatar_url` — прямую ссылку на изображение.",
                        ephemeral=True,
                    )
                    return

                # Списываем деньги и создаём запись
                new_balance = await self.economy.change_balance(interaction.user.id, -price)
                change_id = await self.shop.create_avatar_change(interaction.user.id, avatar_url)

                embed = discord.Embed(
                    title="✅ Покупка совершена",
                    description=f"Аватарка сервера будет установлена на 24 часа!",
                    color=discord.Color.green(),
                )
                embed.add_field(name="ID покупки", value=str(change_id), inline=True)
                embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=True)
                embed.add_field(name="URL", value=avatar_url, inline=False)
                await interaction.response.send_message(embed=embed, ephemeral=True)

            elif item == "lottery_ticket":
                # Списываем деньги и добавляем билет
                new_balance = await self.economy.change_balance(interaction.user.id, -price)
                ticket_number = await self.shop.add_lottery_ticket(interaction.user.id)
                active_tickets = len(await self.shop.get_active_lottery_tickets())

                embed = discord.Embed(
                    title="🎫 Лотерейный билет куплен!",
                    color=discord.Color.green(),
                )
                embed.add_field(name="Номер билета", value=f"#{ticket_number}", inline=True)
                embed.add_field(name="Участников сейчас", value=str(active_tickets), inline=True)
                embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=False)
                embed.set_footer(text="Розыгрыш проводится администрацией. Джекпот = 80% от всех билетов!")
                await interaction.response.send_message(embed=embed, ephemeral=True)

            elif item == "daily_booster":
                # Проверяем, есть ли уже перманентный удвоитель
                already_has = await self.shop.has_daily_booster(interaction.user.id)

                # Списываем деньги и добавляем удвоитель
                new_balance = await self.economy.change_balance(interaction.user.id, -price)
                await self.shop.add_daily_booster(interaction.user.id)

                if already_has:
                    # Если уже был — информируем что купил ещё один (бессмысленно но возможно)
                    embed = discord.Embed(
                        title="⚡ Удвоитель бонуса (ПЕРМАНЕНТНЫЙ)",
                        description=f"У вас уже есть перманентный удвоитель! Вы купили ещё один на всякий случай...",
                        color=discord.Color.orange(),
                    )
                    embed.add_field(name="Ваш /daily теперь даёт", value=f"**{DAILY_REWARD_AMOUNT * 2}** {CURRENCY_EMOJI} навсегда!", inline=False)
                    embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=False)
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    embed = discord.Embed(
                        title="⚡ Удвоитель бонуса активирован! (ПЕРМАНЕНТНЫЙ)",
                        description=f"🎉 Поздравляем! Теперь каждый `/daily` будет давать вам **{DAILY_REWARD_AMOUNT * 2}** {CURRENCY_EMOJI} вместо {DAILY_REWARD_AMOUNT}!",
                        color=discord.Color.green(),
                    )
                    embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=False)
                    embed.set_footer(text="Эффект действует навсегда! Больше не нужно покупать удвоитель.")
                    await interaction.response.send_message(embed=embed, ephemeral=True)

            elif item == "999_rubles":
                # Выгодная сделка: платим 1000, получаем 999
                new_balance = await self.economy.change_balance(interaction.user.id, -price)
                reward = item_data["reward"]
                final_balance = await self.economy.change_balance(interaction.user.id, reward)

                embed = discord.Embed(
                    title="💸 Выгодная сделка завершена!",
                    description=f"Вы заплатили **{price}** {CURRENCY_EMOJI} и получили **{reward}** {CURRENCY_EMOJI}!",
                    color=discord.Color.green(),
                )
                embed.add_field(name="Ваш убыток", value=f"{price - reward} {CURRENCY_EMOJI}", inline=True)
                embed.add_field(name="Новый баланс", value=f"{final_balance} {CURRENCY_EMOJI}", inline=True)
                embed.set_footer(text="Спасибо за покупку! Приходите ещё!")
                await interaction.response.send_message(embed=embed, ephemeral=True)

            elif item == "custom_emoji":
                if not emoji_name or not emoji_url:
                    await interaction.response.send_message(
                        "Для покупки эмодзи-слота укажите `emoji_name` (2-32 символа) и `emoji_url` (прямая ссылка на изображение 128x128 PNG/JPG).",
                        ephemeral=True,
                    )
                    return

                # Проверяем длину названия
                if len(emoji_name) < 2 or len(emoji_name) > 32:
                    await interaction.response.send_message(
                        "Название эмодзи должно быть от 2 до 32 символов.",
                        ephemeral=True,
                    )
                    return

                # Списываем деньги и создаём заявку
                new_balance = await self.economy.change_balance(interaction.user.id, -price)
                request_id = await self.shop.create_emoji_request(
                    interaction.user.id, emoji_name, emoji_url
                )

                embed = discord.Embed(
                    title="😀 Заявка на кастомный эмодзи создана",
                    color=discord.Color.green(),
                )
                embed.add_field(name="ID заявки", value=str(request_id), inline=True)
                embed.add_field(name="Название", value=emoji_name, inline=True)
                embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=False)
                embed.add_field(name="URL изображения", value=emoji_url[:100] + "..." if len(emoji_url) > 100 else emoji_url, inline=False)
                embed.set_footer(text="Администрация рассмотрит заявку и добавит эмодзи на сервер.")
                await interaction.response.send_message(embed=embed, ephemeral=True)

            elif item == "joke_mute":
                if not target_user:
                    await interaction.response.send_message(
                        "Укажите `target_user` — пользователя, которого хотите замутить на 5 минут.",
                        ephemeral=True,
                    )
                    return

                # Проверяем, что не мутим себя
                if target_user.id == interaction.user.id:
                    await interaction.response.send_message(
                        "❌ Нельзя замутить самого себя!",
                        ephemeral=True,
                    )
                    return

                # Проверяем, что не мутим бота
                if target_user.id == self.user.id:
                    await interaction.response.send_message(
                        "❌ Нельзя замутить бота!",
                        ephemeral=True,
                    )
                    return

                # Проверяем права бота
                if not interaction.guild:
                    await interaction.response.send_message(
                        "❌ Эта команда работает только на сервере.",
                        ephemeral=True,
                    )
                    return

                bot_member = interaction.guild.me
                if not bot_member.guild_permissions.moderate_members:
                    await interaction.response.send_message(
                        "❌ У бота нет прав на модерацию участников. Нужно разрешение 'Moderate Members'.",
                        ephemeral=True,
                    )
                    return

                # Проверяем, что бот может замутить целевого пользователя (роль бота выше роли цели)
                if bot_member.top_role <= target_user.top_role:
                    await interaction.response.send_message(
                        "❌ Бот не может замутить этого пользователя — его роль слишком высокая.",
                        ephemeral=True,
                    )
                    return

                # Проверяем, что целевой пользователь не уже замучен
                if target_user.is_timed_out():
                    await interaction.response.send_message(
                        "❌ Этот пользователь уже замучен.",
                        ephemeral=True,
                    )
                    return

                # Списываем деньги
                new_balance = await self.economy.change_balance(interaction.user.id, -price)

                # Применяем таймаут на 5 минут (300 секунд)
                from datetime import datetime, timedelta
                timeout_until = datetime.utcnow() + timedelta(seconds=300)
                await target_user.timeout(timeout_until, reason=f"Мут «ради шутки» куплен {interaction.user.display_name}")

                embed = discord.Embed(
                    title="🔇 Мут «ради шутки» применён!",
                    description=f"{target_user.mention} замучен на 5 минут!",
                    color=discord.Color.red(),
                )
                embed.add_field(name="Куплено за", value=f"{price} {CURRENCY_EMOJI}", inline=True)
                embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=True)
                embed.set_image(url="https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExcDdtZzVxdjQ2YjRjMnJ6aG0wY2J3dGZ1dGZ1dGZ1dGZ1dGZ1dGZiZSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/3o7TKSjRrfIPjeiVyM/giphy.gif")
                embed.set_footer(text="Шутка удалась! 😄")
                await interaction.response.send_message(embed=embed)

            elif item == "nickname_change":
                if not target_user or not new_nick:
                    await interaction.response.send_message(
                        "Укажите `target_user` — пользователя, и `new_nick` — новый никнейм (1-32 символа).",
                        ephemeral=True,
                    )
                    return

                # Проверяем длину ника
                if len(new_nick) < 1 or len(new_nick) > 32:
                    await interaction.response.send_message(
                        "Никнейм должен быть от 1 до 32 символов.",
                        ephemeral=True,
                    )
                    return

                # Проверяем права бота
                if not interaction.guild:
                    await interaction.response.send_message(
                        "❌ Эта команда работает только на сервере.",
                        ephemeral=True,
                    )
                    return

                bot_member = interaction.guild.me
                if not bot_member.guild_permissions.manage_nicknames:
                    await interaction.response.send_message(
                        "❌ У бота нет прав на управление никнеймами. Нужно разрешение 'Manage Nicknames'.",
                        ephemeral=True,
                    )
                    return

                # Проверяем, что бот может изменить ник целевого пользователя (роль бота выше роли цели)
                if bot_member.top_role <= target_user.top_role:
                    await interaction.response.send_message(
                        "❌ Бот не может изменить ник этого пользователя — его роль слишком высокая.",
                        ephemeral=True,
                    )
                    return

                # Сохраняем старый ник для сообщения
                old_nick = target_user.nick or target_user.name

                # Списываем деньги
                new_balance = await self.economy.change_balance(interaction.user.id, -price)

                # Меняем ник
                await target_user.edit(nick=new_nick, reason=f"Смена ника куплена {interaction.user.display_name}")

                embed = discord.Embed(
                    title="📝 Смена ника выполнена!",
                    description=f"{target_user.mention} переименован!",
                    color=discord.Color.purple(),
                )
                embed.add_field(name="Старый ник", value=old_nick, inline=True)
                embed.add_field(name="Новый ник", value=new_nick, inline=True)
                embed.add_field(name="Куплено за", value=f"{price} {CURRENCY_EMOJI}", inline=False)
                embed.add_field(name="Новый баланс", value=f"{new_balance} {CURRENCY_EMOJI}", inline=True)
                embed.set_image(url="https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExcDdtZzVxdjQ2YjRjMnJ6aG0wY2J3dGZ1dGZ1dGZ1dGZ1dGZ1dGZiZSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/l0HlNQ03J5Jz3glv2/giphy.gif")
                embed.set_footer(text="Новое имя, новая жизнь! 😄")
                await interaction.response.send_message(embed=embed)

        @self.tree.command(name="role_requests", description="(Админ) список заявок на роли")
        async def role_requests(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            pending = await self.shop.get_pending_role_requests()
            if not pending:
                await interaction.response.send_message(
                    "Нет ожидающих заявок на роли.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title="📋 Заявки на кастомные роли",
                description=f"Ожидают одобрения: {len(pending)}",
                color=discord.Color.orange(),
            )
            for req in pending[:10]:
                embed.add_field(
                    name=f"Заявка #{req['id']} от <@{req['user_id']}>",
                    value=f"Название: {req['role_name']}\nЦвет: {req['role_color']}",
                    inline=False,
                )
            embed.set_footer(text="Одобрить: /approve_role id: | Отклонить: /reject_role id:")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="approve_role", description="(Админ) одобрить заявку на роль")
        @app_commands.describe(request_id="ID заявки", role_id="ID созданной роли (опционально)")
        async def approve_role(
            interaction: discord.Interaction,
            request_id: int,
            role_id: Optional[str] = None,
        ) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            role_id_int = int(role_id) if role_id and role_id.isdigit() else None
            request = await self.shop.approve_role_request(
                request_id, interaction.user.id, role_id_int
            )

            if request:
                await interaction.response.send_message(
                    f"✅ Заявка #{request_id} одобрена. Пользователю <@{request['user_id']}> выдана роль.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"❌ Заявка #{request_id} не найдена или уже обработана.",
                    ephemeral=True,
                )

        @self.tree.command(name="reject_role", description="(Админ) отклонить заявку на роль")
        @app_commands.describe(request_id="ID заявки", reason="Причина отказа")
        async def reject_role(
            interaction: discord.Interaction,
            request_id: int,
            reason: Optional[str] = None,
        ) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            request = await self.shop.reject_role_request(
                request_id, interaction.user.id, reason or ""
            )

            if request:
                await interaction.response.send_message(
                    f"❌ Заявка #{request_id} отклонена. Деньги возвращены пользователю <@{request['user_id']}>.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"❌ Заявка #{request_id} не найдена или уже обработана.",
                    ephemeral=True,
                )

        # ==================== КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ ЭМОДЗИ ====================

        @self.tree.command(name="emoji_requests", description="(Админ) список заявок на эмодзи")
        async def emoji_requests(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            pending = await self.shop.get_pending_emoji_requests()
            if not pending:
                await interaction.response.send_message(
                    "Нет ожидающих заявок на эмодзи.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title="😀 Заявки на кастомные эмодзи",
                description=f"Ожидают одобрения: {len(pending)}",
                color=discord.Color.orange(),
            )
            for req in pending[:10]:
                embed.add_field(
                    name=f"Заявка #{req['id']} от <@{req['user_id']}>",
                    value=f"Название: `{req['emoji_name']}`\nURL: {req['emoji_url'][:50]}...",
                    inline=False,
                )
            embed.set_footer(text="Одобрить: /approve_emoji id: | Отклонить: /reject_emoji id:")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="approve_emoji", description="(Админ) одобрить заявку на эмодзи")
        @app_commands.describe(request_id="ID заявки", emoji_id="ID добавленного эмодзи (опционально)")
        async def approve_emoji(
            interaction: discord.Interaction,
            request_id: int,
            emoji_id: Optional[str] = None,
        ) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            request = await self.shop.approve_emoji_request(
                request_id, interaction.user.id, emoji_id
            )

            if request:
                embed = discord.Embed(
                    title=f"✅ Заявка #{request_id} одобрена",
                    description=f"Эмодзи `{request['emoji_name']}` для <@{request['user_id']}>",
                    color=discord.Color.green(),
                )
                if emoji_id:
                    embed.add_field(name="ID эмодзи", value=emoji_id, inline=True)
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"❌ Заявка #{request_id} не найдена или уже обработана.",
                    ephemeral=True,
                )

        @self.tree.command(name="reject_emoji", description="(Админ) отклонить заявку на эмодзи")
        @app_commands.describe(request_id="ID заявки", reason="Причина отказа")
        async def reject_emoji(
            interaction: discord.Interaction,
            request_id: int,
            reason: Optional[str] = None,
        ) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            request = await self.shop.reject_emoji_request(
                request_id, interaction.user.id, reason or ""
            )

            if request:
                embed = discord.Embed(
                    title=f"❌ Заявка #{request_id} отклонена",
                    description=f"Эмодзи `{request['emoji_name']}` для <@{request['user_id']}>",
                    color=discord.Color.red(),
                )
                if reason:
                    embed.add_field(name="Причина", value=reason, inline=False)
                embed.add_field(
                    name="Возврат",
                    value=f"{SHOP_ITEMS['custom_emoji']['price']} {CURRENCY_EMOJI} возвращены пользователю",
                    inline=False,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"❌ Заявка #{request_id} не найдена или уже обработана.",
                    ephemeral=True,
                )

        @self.tree.command(name="active_avatars", description="(Админ) активные смены аватарок")
        async def active_avatars(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            active = await self.shop.get_active_avatar_changes()
            if not active:
                await interaction.response.send_message(
                    "Нет активных смен аватарок.", ephemeral=True
                )
                return

            from datetime import datetime
            embed = discord.Embed(
                title="🖼️ Активные смены аватарок",
                color=discord.Color.blue(),
            )
            for change in active[:10]:
                expires = datetime.fromtimestamp(change["expires_at"], tz=timezone.utc)
                embed.add_field(
                    name=f"#{change['id']} — <@{change['user_id']}>",
                    value=f"Истекает: <t:{int(change['expires_at'])}:R>\n{change['avatar_url'][:100]}...",
                    inline=False,
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="draw_lottery", description="(Админ) провести розыгрыш лотереи")
        async def draw_lottery(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "Команда доступна только администраторам.", ephemeral=True
                )
                return

            active_tickets = await self.shop.get_active_lottery_tickets()
            if not active_tickets:
                await interaction.response.send_message(
                    "Нет активных билетов для розыгрыша.", ephemeral=True
                )
                return

            # Проводим розыгрыш
            winner = await self.shop.draw_lottery_winner()
            if winner:
                jackpot = winner["jackpot"]
                # Начисляем джекпот победителю
                await self.economy.change_balance(winner["user_id"], jackpot)

                embed = discord.Embed(
                    title="🎉 Розыгрыш лотереи завершён!",
                    description=f"Победитель: <@{winner['user_id']}>",
                    color=discord.Color.gold(),
                )
                embed.add_field(name="Джекпот", value=f"**{jackpot}** {CURRENCY_EMOJI}", inline=True)
                embed.add_field(name="Участников", value=str(len(active_tickets)), inline=True)
                embed.set_footer(text=f"Провёл: {interaction.user.display_name}")
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message(
                    "Ошибка при проведении розыгрыша.", ephemeral=True
                )

        @self.tree.command(name="lottery_status", description="Статус лотереи и количество билетов")
        async def lottery_status(interaction: discord.Interaction) -> None:
            active = await self.shop.get_active_lottery_tickets()
            participants = len(set(t["user_id"] for t in active))
            total_tickets = len(active)
            jackpot = total_tickets * SHOP_ITEMS["lottery_ticket"]["price"] * 8 // 10

            embed = discord.Embed(
                title="🎫 Статус лотереи",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Билетов куплено", value=str(total_tickets), inline=True)
            embed.add_field(name="Участников", value=str(participants), inline=True)
            embed.add_field(name="Текущий джекпот", value=f"**{jackpot}** {CURRENCY_EMOJI}", inline=True)
            embed.add_field(name="Цена билета", value=f"{SHOP_ITEMS['lottery_ticket']['price']} {CURRENCY_EMOJI}", inline=True)
            embed.set_footer(text="Купить билет: /buy lottery_ticket")
            await interaction.response.send_message(embed=embed)

        # ==================== КОМАНДЫ ТОТАЛИЗАТОРА ====================

        @self.tree.command(name="totals_create", description="(Админ) Создать событие для ставок")
        @app_commands.describe(
            title="Название события",
            description="Описание события",
            options="Варианты исхода через запятую (например: Да, Нет, Неизвестно)",
            min_bet="Минимальная ставка (по умолчанию 100)",
            max_bet="Максимальная ставка (по умолчанию 10000)",
        )
        async def totals_create(
            interaction: discord.Interaction,
            title: str,
            description: str,
            options: str,
            min_bet: int = TOTALS_DEFAULT_MIN_BET,
            max_bet: int = TOTALS_DEFAULT_MAX_BET,
        ) -> None:
            """Создаёт новое событие для ставок тотализатора."""
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "❌ Команда доступна только администраторам.", ephemeral=True
                )
                return

            # Парсим варианты
            options_list = [opt.strip() for opt in options.split(",") if opt.strip()]
            if len(options_list) < 2:
                await interaction.response.send_message(
                    "❌ Нужно указать минимум 2 варианта через запятую.", ephemeral=True
                )
                return

            if len(options_list) > 10:
                await interaction.response.send_message(
                    "❌ Максимум 10 вариантов.", ephemeral=True
                )
                return

            event_id = await self.totals.create_event(
                creator_id=interaction.user.id,
                title=title,
                description=description,
                options=options_list,
                min_bet=min_bet,
                max_bet=max_bet,
            )

            # Создаём embed с информацией о событии
            embed = discord.Embed(
                title=f"🏆 Событие #{event_id}: {title}",
                description=description,
                color=discord.Color.green(),
            )
            embed.add_field(name="Варианты исхода:", value="\n".join(f"{i+1}. {opt}" for i, opt in enumerate(options_list)))
            embed.add_field(name="Ставки:", value=f"от {min_bet} до {max_bet} {CURRENCY_EMOJI}", inline=False)
            embed.add_field(name="Как ставить:", value=f"/totals_bet event_id:{event_id} option:1 amount:1000", inline=False)
            embed.set_footer(text=f"Создано: {interaction.user.display_name}")

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="totals_bet", description="Сделать ставку на событие")
        @app_commands.describe(
            event_id="ID события",
            option="Номер варианта (1, 2, 3...)",
            amount="Сумма ставки",
        )
        async def totals_bet(
            interaction: discord.Interaction,
            event_id: int,
            option: int,
            amount: int,
        ) -> None:
            """Делает ставку на активное событие."""
            # Проверяем баланс
            balance = await self.economy.ensure_account(interaction.user.id)
            if balance < amount:
                await interaction.response.send_message(
                    f"❌ Недостаточно средств. Баланс: {balance} {CURRENCY_EMOJI}", ephemeral=True
                )
                return

            # Делаем ставку (option-1 т.к. индексация с 0)
            result = await self.totals.place_bet(event_id, interaction.user.id, option - 1, amount)

            if result is None:
                await interaction.response.send_message(
                    "❌ Событие не найдено или уже закрыто.", ephemeral=True
                )
                return

            if "error" in result:
                error = result["error"]
                if error == "bet_range":
                    await interaction.response.send_message(
                        f"❌ Ставка должна быть от {result['min']} до {result['max']} {CURRENCY_EMOJI}",
                        ephemeral=True,
                    )
                elif error == "invalid_option":
                    await interaction.response.send_message("❌ Неверный вариант.", ephemeral=True)
                elif error == "insufficient_funds":
                    await interaction.response.send_message(
                        f"❌ Недостаточно средств. Баланс: {result['balance']} {CURRENCY_EMOJI}",
                        ephemeral=True,
                    )
                elif error == "already_bet":
                    await interaction.response.send_message(
                        "❌ Вы уже сделали ставку на этот вариант.", ephemeral=True
                    )
                return

            # Успешная ставка
            event = result["event"]
            embed = discord.Embed(
                title="✅ Ставка принята!",
                color=discord.Color.green(),
            )
            embed.add_field(name="Событие", value=f"#{event_id}: {event['title']}", inline=False)
            embed.add_field(name="Вариант", value=event["options"][option - 1], inline=True)
            embed.add_field(name="Ставка", value=f"{amount} {CURRENCY_EMOJI}", inline=True)
            embed.add_field(name="Общий пул", value=f"{event['total_pool']} {CURRENCY_EMOJI}", inline=True)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="totals_list", description="Список активных событий для ставок")
        async def totals_list(interaction: discord.Interaction) -> None:
            """Показывает список активных событий тотализатора."""
            active_events = await self.totals.get_active_events()

            if not active_events:
                await interaction.response.send_message(
                    "📭 Нет активных событий. Администраторы могут создать через `/totals_create`",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="🏆 Активные события тотализатора",
                color=discord.Color.blue(),
            )

            for event in active_events[:10]:  # Максимум 10 событий
                options_text = "\n".join(
                    f"{i+1}. {opt}" for i, opt in enumerate(event["options"])
                )
                embed.add_field(
                    name=f"#{event['id']}: {event['title']}",
                    value=(
                        f"{event['description'][:100]}...\n"
                        f"Варианты:\n{options_text}\n"
                        f"Ставок: {len(event['bets'])} | Пул: {event['total_pool']} {CURRENCY_EMOJI}\n"
                        f"Ставить: `/totals_bet {event['id']} 1 1000`"
                    ),
                    inline=False,
                )

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="totals_status", description="Статус события и текущие ставки")
        @app_commands.describe(event_id="ID события")
        async def totals_status(interaction: discord.Interaction, event_id: int) -> None:
            """Показывает подробную информацию о событии и ставки."""
            status = await self.totals.get_event_status(event_id)

            if not status:
                await interaction.response.send_message(
                    "❌ Событие не найдено.", ephemeral=True
                )
                return

            event = status["event"]
            stats = status["stats"]

            # Определяем цвет и статус
            status_colors = {
                "active": discord.Color.green(),
                "closed": discord.Color.gold(),
                "cancelled": discord.Color.red(),
            }
            status_texts = {
                "active": "🟢 Активно",
                "closed": "🔒 Закрыто",
                "cancelled": "❌ Отменено",
            }

            embed = discord.Embed(
                title=f"🏆 Событие #{event_id}: {event['title']}",
                description=event["description"],
                color=status_colors.get(event["status"], discord.Color.grey),
            )

            # Добавляем статистику по вариантам
            for i, option in enumerate(event["options"]):
                stat = stats[i]
                winner_mark = " 👑" if event.get("winning_option") == i else ""
                embed.add_field(
                    name=f"{i+1}. {option}{winner_mark}",
                    value=f"Ставок: {stat['total_bets']} | Сумма: {stat['total_amount']} {CURRENCY_EMOJI}",
                    inline=True,
                )

            embed.add_field(
                name="Общий пул",
                value=f"**{event['total_pool']}** {CURRENCY_EMOJI}",
                inline=False,
            )
            embed.add_field(
                name="Статус",
                value=status_texts.get(event["status"], event["status"]),
                inline=True,
            )
            embed.set_footer(text=f"Создано: <@{event['creator_id']}> | Всего ставок: {len(event['bets'])}")

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="totals_close", description="(Админ) Закрыть событие и выплатить выигрыши")
        @app_commands.describe(
            event_id="ID события",
            winning_option="Номер победившего варианта (1, 2, 3...)",
        )
        async def totals_close(
            interaction: discord.Interaction,
            event_id: int,
            winning_option: int,
        ) -> None:
            """Закрывает событие и распределяет выигрыши."""
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "❌ Команда доступна только администраторам.", ephemeral=True
                )
                return

            result = await self.totals.close_event(event_id, winning_option - 1)

            if not result:
                await interaction.response.send_message(
                    "❌ Событие не найдено или уже закрыто.", ephemeral=True
                )
                return

            event = result["event"]
            winners = result["winners"]
            winning_option_text = event["options"][winning_option - 1]

            embed = discord.Embed(
                title=f"🔒 Событие #{event_id} закрыто",
                description=f"Победивший вариант: **{winning_option}. {winning_option_text}**",
                color=discord.Color.gold(),
            )

            if winners:
                winners_text = "\n".join(
                    f"<@{w['user_id']}>: ставка {w['bet']} → выигрыш **{w['win']}** {CURRENCY_EMOJI}"
                    for w in winners[:10]
                )
                if len(winners) > 10:
                    winners_text += f"\n_и ещё {len(winners) - 10} победителей..._"
                embed.add_field(name="🏆 Победители:", value=winners_text, inline=False)
                total_payout = sum(w["win"] for w in winners)
                embed.add_field(name="Выплачено", value=f"{total_payout} {CURRENCY_EMOJI}", inline=True)
            else:
                embed.add_field(name="🏆 Победители", value="Нет ставок на победивший вариант. Пул ушёл банку.")

            embed.add_field(name="Общий пул", value=f"{event['total_pool']} {CURRENCY_EMOJI}", inline=True)
            embed.set_footer(text=f"Закрыто: {interaction.user.display_name}")

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="totals_cancel", description="(Админ) Отменить событие и вернуть ставки")
        @app_commands.describe(event_id="ID события")
        async def totals_cancel(interaction: discord.Interaction, event_id: int) -> None:
            """Отменяет событие и возвращает все ставки."""
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "❌ Команда доступна только администраторам.", ephemeral=True
                )
                return

            result = await self.totals.cancel_event(event_id)

            if not result:
                await interaction.response.send_message(
                    "❌ Событие не найдено или уже закрыто.", ephemeral=True
                )
                return

            event = result["event"]
            refunded = result["refunded_count"]

            embed = discord.Embed(
                title=f"❌ Событие #{event_id} отменено",
                description=f"{event['title']}\n\nВозвращено ставок: **{refunded}**",
                color=discord.Color.red(),
            )
            embed.add_field(
                name="Все ставки возвращены",
                value=f"Общая сумма: {event['total_pool']} {CURRENCY_EMOJI}",
            )
            embed.set_footer(text=f"Отменено: {interaction.user.display_name}")

            await interaction.response.send_message(embed=embed)

        # ==================== КОМАНДЫ ФАРМА ЗА СООБЩЕНИЯ ====================

        @self.tree.command(name="set_farm_channel", description="(Админ) Установить канал для фарма за сообщения")
        @app_commands.describe(channel="Канал, в котором будут начисляться деньги за сообщения")
        async def set_farm_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ) -> None:
            """Устанавливает канал для фарма за сообщения."""
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "❌ Команда доступна только администраторам.", ephemeral=True
                )
                return

            self.farm_channel_id = channel.id
            self._save_farm_settings()

            embed = discord.Embed(
                title="💬 Канал для фарма установлен",
                description=f"Теперь за каждые **{FARM_MESSAGES_REQUIRED}** сообщений в канале {channel.mention} "
                            f"пользователи получают **{FARM_REWARD} {CURRENCY_EMOJI}**!",
                color=discord.Color.green(),
            )
            embed.add_field(name="Требуется сообщений", value=str(FARM_MESSAGES_REQUIRED), inline=True)
            embed.add_field(name="Награда", value=f"{FARM_REWARD} {CURRENCY_EMOJI}", inline=True)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="farm_status", description="Проверить статус фарма за сообщения")
        async def farm_status(interaction: discord.Interaction) -> None:
            """Показывает текущий статус фарма за сообщения."""
            if not self.farm_channel_id:
                await interaction.response.send_message(
                    "❌ Канал для фарма не настроен. Администратор может установить его через `/set_farm_channel`",
                    ephemeral=True,
                )
                return

            # Проверяем, установлен ли канал на текущем сервере
            farm_channel = interaction.guild.get_channel(self.farm_channel_id) if interaction.guild else None
            if not farm_channel:
                await interaction.response.send_message(
                    "❌ Канал для фарма настроен, но не найден на этом сервере.",
                    ephemeral=True,
                )
                return

            user_id = interaction.user.id
            current_count = self.message_counters.get(user_id, 0)
            remaining = FARM_MESSAGES_REQUIRED - current_count

            embed = discord.Embed(
                title="💬 Статус фарма за сообщения",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Канал для фарма", value=farm_channel.mention, inline=False)
            embed.add_field(name="Ваш прогресс", value=f"{current_count}/{FARM_MESSAGES_REQUIRED} сообщений", inline=True)
            embed.add_field(name="Осталось до награды", value=f"{remaining} сообщений", inline=True)
            embed.add_field(name="Награда", value=f"{FARM_REWARD} {CURRENCY_EMOJI}", inline=True)

            # Прогресс-бар
            progress = min(current_count / FARM_MESSAGES_REQUIRED, 1.0)
            filled = int(progress * 10)
            empty = 10 - filled
            progress_bar = "█" * filled + "░" * empty
            embed.add_field(name="Прогресс", value=f"`{progress_bar}` {int(progress * 100)}%", inline=False)

            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="reset_farm_counters", description="(Админ) Сбросить счётчики сообщений всех пользователей")
        async def reset_farm_counters(interaction: discord.Interaction) -> None:
            """Сбрасывает все счётчики сообщений."""
            if not self._is_admin(interaction.user.id):
                await interaction.response.send_message(
                    "❌ Команда доступна только администраторам.", ephemeral=True
                )
                return

            self.message_counters.clear()
            self._save_farm_settings()

            await interaction.response.send_message(
                "✅ Счётчики сообщений сброшены для всех пользователей.",
                ephemeral=True,
            )

    def _load_farm_settings(self) -> None:
        """Загружает настройки фарма из файла."""
        try:
            if FARM_FILE.exists():
                with open(FARM_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.farm_channel_id = data.get("farm_channel_id")
                    # Загружаем счётчики сообщений
                    counters = data.get("message_counters", {})
                    self.message_counters = defaultdict(int, {int(k): v for k, v in counters.items()})
            else:
                self.farm_channel_id = None
                self.message_counters = defaultdict(int)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            self.farm_channel_id = None
            self.message_counters = defaultdict(int)

    def _save_farm_settings(self) -> None:
        """Сохраняет настройки фарма в файл."""
        try:
            DATA_DIR.mkdir(exist_ok=True)
            data = {
                "farm_channel_id": self.farm_channel_id,
                "message_counters": dict(self.message_counters),
            }
            with open(FARM_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек фарма: {e}")

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
