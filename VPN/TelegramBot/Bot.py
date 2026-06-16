"""Telegram bot entrypoint using aiogram."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "Server_config.json"
USERS_DIR = ROOT_DIR / "users"

sys.path.append(str(ROOT_DIR))
from backend.backend import (  # noqa: E402
    create_subscription,
    get_mtproto_proxy_for_server,
    list_user_subscriptions,
    load_config,
    load_user,
    maybe_deploy_xray,
    maybe_restart_xray,
    run_sub_server,
    save_user,
    write_xray_config,
)

PENDING_PURCHASE: dict[int, tuple[str, int]] = {}
PENDING_CONTEXT: dict[int, dict[str, Any]] = {}

async def safe_answer(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

def get_bot_token(cfg: dict[str, Any]) -> str:
    env_token = str(os.getenv("HMAO_BOT_TOKEN") or "").strip()
    if env_token:
        return env_token
    token = str(cfg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("bot_token is missing in config/Server_config.json and HMAO_BOT_TOKEN")
    return token



def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


def fmt_remaining(expires_at: int | None) -> str:
    if not expires_at:
        return "без срока"
    now = int(datetime.now().timestamp())
    delta = int(expires_at) - now
    if delta <= 0:
        return "истекла"
    minutes = max(1, delta // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, mins = divmod(minutes, 60)
    if mins:
        return f"{hours} ч {mins} мин"
    return f"{hours} ч"


def fmt_term(minutes: int | None, months: int | None) -> str:
    if minutes:
        return f"{minutes} мин."
    if months:
        return f"{months} мес."
    return "—"




def get_plan_price(cfg: dict, months: int) -> int | None:
    plans = cfg.get("subscription_plans") or []
    for p in plans:
        try:
            if int(p.get("months")) == int(months):
                return int(p.get("price_rub"))
        except Exception:
            continue
    prices = cfg.get("subscription_prices_rub") or {}
    if isinstance(prices, dict):
        val = prices.get(str(months))
        if val is not None:
            try:
                return int(val)
            except Exception:
                return None
    return None

def active_subscription_info(subs: list[dict[str, Any]], server_id: str) -> tuple[bool, int | None]:
    has_active = False
    expiries: list[int] = []
    for sub in subs:
        if sub.get("server_id") != server_id:
            continue
        if not sub.get("active"):
            continue
        has_active = True
        exp = sub.get("expires_at")
        if exp:
            expiries.append(int(exp))
        else:
            return True, None
    if not has_active:
        return False, None
    if expiries:
        return True, max(expiries)
    return True, None


def _is_expired(sub: dict[str, Any], now_ts: int) -> bool:
    exp = sub.get("expires_at")
    if not exp:
        return False
    try:
        return int(exp) <= now_ts
    except Exception:
        return False


def _iter_user_ids() -> list[int]:
    if not USERS_DIR.exists():
        return []
    ids: list[int] = []
    for path in USERS_DIR.glob("*.json"):
        try:
            ids.append(int(path.stem))
        except Exception:
            continue
    return ids


def _sync_server(server_id: str, cfg: dict) -> str:
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        return f"Сервер не найден: {server_id}"
    try:
        config_path = write_xray_config(server_id)
    except Exception as exc:
        return "Xray конфиг не обновлён: " + str(exc)
    if server.get("managed_local", False):
        _, msg = maybe_restart_xray(config_path)
        return msg
    _, msg = maybe_deploy_xray(server_id, config_path)
    return msg


def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="\U0001F6F0\ufe0f \u0421\u0435\u0440\u0432\u0435\u0440\u0430"),
                KeyboardButton(text="\U0001F9FE \u041c\u043e\u044f \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0430"),
            ],
            [
                KeyboardButton(text="\U0001F4B3 \u041a\u0443\u043f\u0438\u0442\u044c \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0443"),
                KeyboardButton(text="\U0001F511 \u041c\u043e\u0438 \u043a\u043b\u044e\u0447\u0438"),
            ],
            [KeyboardButton(text="\u2708\ufe0f Telegram Proxy")],
        ],
        resize_keyboard=True,
    )

def server_label(server: dict | None) -> str:
    if not server:
        return ""
    flag = str(server.get("flag") or "").strip()
    name = str(server.get("name") or server.get("id") or "").strip()
    label = " ".join([p for p in [flag, name] if p])
    return label or name


def decorate_sub_url(cfg: dict, sub_url: str) -> str:
    display = str(cfg.get("sub_display_name") or cfg.get("brand_name") or "").strip()
    if not display:
        return sub_url
    return f"{sub_url}#{quote(display)}"


def build_device_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Android", callback_data="device:android")],
            [InlineKeyboardButton(text="🪟 Windows", callback_data="device:windows")],
            [InlineKeyboardButton(text="🍎 iOS / macOS", callback_data="device:apple")],
        ]
    )


def build_autoadd_keyboard(cfg: dict, sub_url: str) -> InlineKeyboardMarkup | None:
    template = str(cfg.get("v2raytun_sub_deeplink") or "").strip()
    if template:
        deep_link = template.format(url=quote(sub_url, safe=""))
        if deep_link.startswith(("http://", "https://", "tg://")):
            return InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📲 Автодобавление", url=deep_link)]]
            )
    if sub_url.startswith(("http://", "https://")):
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🌐 Открыть /sub", url=sub_url)]]
        )
    return None

def build_delete_keys_keyboard(subs: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for i, sub in enumerate(subs, start=1):
        client_id = sub.get("client_id")
        if not client_id:
            continue
        status = "✅" if sub.get("active") else "⛔"
        label = f"🗑️ {i} {status} {sub.get('server_name')}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"delkey:{client_id}")])
    buttons.append([InlineKeyboardButton(text="➕ Новый ключ", callback_data="newkey")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_v2raytun_import_link(value: str) -> str:
    return f"v2raytun://import/{quote(value, safe='')}"


def device_instructions(device: str) -> str:
    if device == "android":
        return (
            "Инструкция (Android):\n"
            "1. Установите v2RayTun.\n"
            "2. Откройте приложение → Подписки → Добавить.\n"
            "3. Вставьте /sub ссылку и обновите список.\n"
        )
    if device == "windows":
        return (
            "Инструкция (Windows):\n"
            "1. Установите v2RayTun.\n"
            "2. Откройте приложение → Subscriptions → Add.\n"
            "3. Вставьте /sub ссылку и обновите список.\n"
        )
    return (
        "Инструкция (iOS / macOS):\n"
        "1. Установите v2RayTun.\n"
        "2. Откройте приложение → Подписки → Добавить.\n"
        "3. Вставьте /sub ссылку (на iOS работает только HTTPS).\n"
    )


def iter_servers(cfg: dict) -> Iterable[dict]:
    for s in cfg.get("servers", []):
        yield s


def build_servers_keyboard(cfg: dict, action: str = "server") -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for s in iter_servers(cfg):
        server_id = s.get("id", "")
        name = server_label(s) or server_id
        if not server_id:
            continue
        cb = f"{action}:{server_id}"
        label = f"{name}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_telegram_proxy_servers_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for s in iter_servers(cfg):
        server_id = str(s.get("id") or "").strip()
        if not server_id:
            continue
        proxy = get_mtproto_proxy_for_server(cfg, server_id)
        if not proxy:
            continue
        buttons.append([InlineKeyboardButton(text=server_label(s) or server_id, callback_data=f"tgproxy:{server_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_telegram_proxy_text(proxy: dict[str, Any]) -> str:
    return (
        "Telegram MTProto proxy:\n"
        f"Server: {proxy.get('server_name') or proxy.get('server_id')}\n"
        f"Host: {proxy.get('host')}\n"
        f"Port: {proxy.get('port')}\n"
        f"Secret: {proxy.get('secret')}\n"
        f"Quick connect: {proxy.get('web_link')}\n"
        "\nTelegram path: Settings -> Data and Storage -> Proxy -> Add Proxy -> MTProto.\n"
    )

def build_months_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    terms = cfg.get("subscription_terms_minutes") or []
    if terms:
        for m in terms:
            buttons.append(
                [InlineKeyboardButton(text=f"⏱️ {m} мин.", callback_data=f"buy_minutes:{m}")]
            )
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    for m in cfg.get("subscription_terms_months", []):
        price = get_plan_price(cfg, int(m))
        label = f"🗓️ {m} мес."
        if price:
            label = f"{label} — {price} ₽"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"buy_months:{m}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_restart_message(server: dict, config_path: Path) -> str:
    if server.get("managed_local", False):
        restarted, msg = maybe_restart_xray(config_path)
        return msg if restarted else "Автоперезапуск Xray выключен."
    server_id = str(server.get("id") or "")
    deployed, msg = maybe_deploy_xray(server_id, config_path)
    if deployed:
        return msg
    return "Сервер удалённый: загрузите этот конфиг на VPS и перезапустите Xray (или настройте deploy)."


async def subscription_watcher(bot: Bot) -> None:
    while True:
        cfg = load_config()
        interval = int(cfg.get("subscription_check_interval_sec") or 60)
        await asyncio.sleep(interval)
        now_ts = int(time.time())
        affected_servers: set[str] = set()
        for user_id in _iter_user_ids():
            user = load_user(user_id)
            subs = user.get("subscriptions", [])
            changed = False
            for sub in subs:
                if not _is_expired(sub, now_ts):
                    continue
                if sub.get("expired_notified"):
                    continue
                sub["expired_notified"] = True
                sub["expired_notified_at"] = now_ts
                changed = True
                server_id = sub.get("server_id")
                if server_id:
                    affected_servers.add(str(server_id))
                server_name = server_label(
                    next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
                )
                try:
                    await bot.send_message(
                        user_id,
                        "⛔ Подписка истекла.\n" f"Сервер: {server_name or server_id}\n",
                    )
                except Exception:
                    pass

            pending_msgs = user.get("pending_messages", []) or []
            if pending_msgs:
                for msg in pending_msgs:
                    try:
                        await bot.send_message(user_id, str(msg.get("text") or ""))
                    except Exception:
                        pass
                user["pending_messages"] = []
                changed = True

            if changed:
                save_user(user)

        for server_id in affected_servers:
            try:
                _sync_server(server_id, cfg)
            except Exception:
                pass


async def cmd_start(message: Message) -> None:
    await message.answer("Привет! Выбери действие в меню.", reply_markup=build_main_menu())


async def cmd_servers(message: Message) -> None:
    cfg = load_config()
    keyboard = build_servers_keyboard(cfg, action="server")
    if not keyboard.inline_keyboard:
        await message.answer("No servers available yet.")
        return
    await message.answer("Выберите сервер:", reply_markup=keyboard)


async def menu_servers(message: Message) -> None:
    cfg = load_config()
    keyboard = build_servers_keyboard(cfg, action="server")
    await message.answer("🛰️ Выберите сервер:", reply_markup=keyboard)


async def menu_my_subscription(message: Message) -> None:
    user = load_user(message.from_user.id)
    active_id = user.get("active_sub_id")
    if not active_id:
        await message.answer("🧾 У вас пока нет активной подписки.", reply_markup=build_main_menu())
        return
    subs = list_user_subscriptions(message.from_user.id)
    sub = next((s for s in subs if s.get("sub_id") == active_id), None)
    if not sub:
        await message.answer("🧾 Активная подписка не найдена.", reply_markup=build_main_menu())
        return
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == sub.get("server_id")), None)
    server_name = server_label(server) or sub.get("server_id")
    status = "✅ активна" if sub.get("active") else "⛔ истекла"
    text = (
        "🧾 Ваша подписка:\n"
        f"ID: {sub.get('sub_id')}\n"
        f"Сервер: {server_name}\n"
        f"Статус: {status}\n"
        f"Создана: {fmt_ts(sub.get('created_at'))}\n"
        f"Срок: {fmt_term(sub.get('minutes'), sub.get('months'))}\n"
        f"Истекает: {fmt_ts(sub.get('expires_at'))} (осталось: {fmt_remaining(sub.get('expires_at'))})\n"
    )
    await message.answer(text, reply_markup=build_main_menu())


async def menu_buy_subscription(message: Message) -> None:
    cfg = load_config()
    keyboard = build_months_keyboard(cfg)
    await message.answer("💳 Выберите срок подписки (пока бесплатно):", reply_markup=keyboard)


async def menu_telegram_proxy(message: Message) -> None:
    cfg = load_config()
    keyboard = build_telegram_proxy_servers_keyboard(cfg)
    if not keyboard.inline_keyboard:
        await message.answer("MTProto is not configured on any server yet.", reply_markup=build_main_menu())
        return
    await message.answer("Choose a server for Telegram MTProto:", reply_markup=keyboard)

async def menu_my_keys(message: Message) -> None:
    subs = list_user_subscriptions(message.from_user.id)
    if not subs:
        await message.answer(
            "No keys yet. Press Servers to create one.",
            reply_markup=build_main_menu(),
        )
        return
    cfg = load_config()
    lines: list[str] = ["Your keys:\n"]
    for i, sub in enumerate(subs, start=1):
        sub_url = decorate_sub_url(cfg, sub.get("sub_url", ""))
        status = "OK" if sub.get("active") else "EXPIRED"
        lines.append(
            f"{i}) {status} {sub.get('server_name')}\n"
            f"Created: {fmt_ts(sub.get('created_at'))}\n"
            f"Expires: {fmt_ts(sub.get('expires_at'))} (left: {fmt_remaining(sub.get('expires_at'))})\n"
            f"/sub: {sub_url}\n"
        )
        proxy = sub.get("telegram_proxy") or {}
        proxy_link = str(proxy.get("web_link") or "").strip()
        if proxy_link:
            lines.append(f"Telegram MTProto: {proxy_link}\n")
    kb = build_delete_keys_keyboard(subs)
    await message.answer("\n".join(lines), reply_markup=kb)

async def on_server_pick(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, server_id = callback.data.split(":", 1)
    user_id = callback.from_user.id
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        await callback.message.answer("?????? ?? ??????.")
        await safe_answer(callback)
        return
    subs = list_user_subscriptions(user_id)
    active, expires_at = active_subscription_info(subs, server_id)
    if not active:
        keyboard = build_months_keyboard(cfg)
        await callback.message.answer(
            "⛔ Нет активной подписки на этот сервер. Сначала купите доступ.",
            reply_markup=keyboard,
        )
        await safe_answer(callback)
        return
    PENDING_CONTEXT[user_id] = {"server_id": server_id, "expires_at": expires_at}
    await callback.message.answer("Выберите устройство:", reply_markup=build_device_keyboard())
    await safe_answer(callback)


async def on_tgproxy_pick(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, server_id = callback.data.split(":", 1)
    cfg = load_config()
    proxy = get_mtproto_proxy_for_server(cfg, server_id)
    if not proxy:
        await callback.message.answer("MTProto is not configured for this server.")
        await safe_answer(callback)
        return
    await callback.message.answer(build_telegram_proxy_text(proxy), disable_web_page_preview=True)
    await safe_answer(callback)

async def on_disabled_server(callback: CallbackQuery) -> None:
    cfg = load_config()
    await callback.message.answer("??????? ?????? ????????:", reply_markup=build_servers_keyboard(cfg, action="server"))
    await safe_answer(callback)


async def on_new_key(callback: CallbackQuery) -> None:
    cfg = load_config()
    keyboard = build_servers_keyboard(cfg, action="server")
    await callback.message.answer("Выберите сервер для нового ключа:", reply_markup=keyboard)
    await safe_answer(callback)


async def on_buy_months(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, months_str = callback.data.split(":", 1)
    try:
        months = int(months_str)
    except ValueError:
        await safe_answer(callback)
        return
    PENDING_PURCHASE[callback.from_user.id] = ("months", months)
    cfg = load_config()
    keyboard = build_servers_keyboard(cfg, action="buyserver")
    await callback.message.answer("Выберите сервер для подписки:", reply_markup=keyboard)
    await safe_answer(callback)


async def on_buy_minutes(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, minutes_str = callback.data.split(":", 1)
    try:
        minutes = int(minutes_str)
    except ValueError:
        await safe_answer(callback)
        return
    PENDING_PURCHASE[callback.from_user.id] = ("minutes", minutes)
    cfg = load_config()
    keyboard = build_servers_keyboard(cfg, action="buyserver")
    await callback.message.answer("Выберите сервер для подписки:", reply_markup=keyboard)
    await safe_answer(callback)


async def on_buy_server(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, server_id = callback.data.split(":", 1)
    user_id = callback.from_user.id
    purchase = PENDING_PURCHASE.pop(user_id, None)
    if not purchase:
        await callback.message.answer("Сначала выберите срок подписки.")
        await safe_answer(callback)
        return
    kind, value = purchase
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        await callback.message.answer("?????? ?? ??????.")
        await safe_answer(callback)
        return
    ctx = {"server_id": server_id}
    if kind == "minutes":
        ctx["minutes"] = value
    else:
        ctx["months"] = value
    PENDING_CONTEXT[user_id] = ctx
    await callback.message.answer("Выберите устройство:", reply_markup=build_device_keyboard())
    await safe_answer(callback)


async def on_device_pick(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, device = callback.data.split(":", 1)
    ctx = PENDING_CONTEXT.pop(callback.from_user.id, None)
    if not ctx:
        await callback.message.answer("Сначала выберите сервер.")
        await safe_answer(callback)
        return
    server_id = ctx.get("server_id")
    if not server_id:
        await callback.message.answer("Сначала выберите сервер.")
        await safe_answer(callback)
        return
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    minutes = ctx.get("minutes")
    months = ctx.get("months")
    expires_at = ctx.get("expires_at")

    result = create_subscription(
        user_id=callback.from_user.id,
        server_id=server_id,
        months=months,
        minutes=minutes,
        expires_at=expires_at,
    )
    sub_url = decorate_sub_url(cfg, result["url"])
    try:
        config_path = write_xray_config(server_id)
    except Exception as exc:
        await callback.message.answer("Подписка создана, но Xray конфиг не обновлён: " + str(exc))
        await safe_answer(callback)
        return
    restart_msg = build_restart_message(server, config_path)
    text = (
        device_instructions(device)
        + "\n/sub: "
        + sub_url
        + "\n"
        + f"Истекает: {fmt_ts(result['sub'].get('expires_at'))} (осталось: {fmt_remaining(result['sub'].get('expires_at'))})\n"
        + "\nXray конфиг обновлен: "
        + str(config_path)
        + "\n"
        + restart_msg
    )
    await callback.message.answer(text, reply_markup=build_autoadd_keyboard(cfg, sub_url))
    telegram_proxy = result.get("telegram_proxy") or {}
    if telegram_proxy:
        await callback.message.answer(build_telegram_proxy_text(telegram_proxy), disable_web_page_preview=True)
    await safe_answer(callback)
async def on_delete_key(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, client_id = callback.data.split(":", 1)
    user_id = callback.from_user.id
    user = load_user(user_id)
    subs = user.get("subscriptions", []) or []
    removed = [s for s in subs if str(s.get("client_id")) == str(client_id)]
    if not removed:
        await callback.message.answer("Ключ не найден.")
        await safe_answer(callback)
        return
    user["subscriptions"] = [s for s in subs if str(s.get("client_id")) != str(client_id)]
    user["clients"] = [c for c in user.get("clients", []) if str(c.get("client_id")) != str(client_id)]
    removed_sub_ids = {s.get("sub_id") for s in removed if s.get("sub_id")}
    if user.get("active_sub_id") in removed_sub_ids:
        remaining = user.get("subscriptions", [])
        user["active_sub_id"] = remaining[-1].get("sub_id") if remaining else None
    save_user(user)

    cfg = load_config()
    server_ids = {s.get("server_id") for s in removed if s.get("server_id")}
    for server_id in server_ids:
        try:
            _sync_server(str(server_id), cfg)
        except Exception:
            pass

    await callback.message.answer("✅ Ключ удалён.")
    await safe_answer(callback)

async def on_unknown_message(message: Message) -> None:
    await message.answer(
        "I did not understand this message. Press /start and use menu buttons.",
        reply_markup=build_main_menu(),
    )

async def on_unknown_callback(callback: CallbackQuery) -> None:
    await callback.message.answer("This button is outdated. Press /start and try again.")
    await safe_answer(callback)

def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_servers, Command("servers"))

    # Strict button texts
    dp.message.register(menu_servers, F.text.in_(["Servers", "\U0001F6F0\ufe0f \u0421\u0435\u0440\u0432\u0435\u0440\u0430"]))
    dp.message.register(
        menu_my_subscription,
        F.text.in_(["My Subscription", "\U0001F9FE \u041c\u043e\u044f \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0430"]),
    )
    dp.message.register(
        menu_buy_subscription,
        F.text.in_(["Buy Subscription", "\U0001F4B3 \u041a\u0443\u043f\u0438\u0442\u044c \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0443"]),
    )
    dp.message.register(menu_my_keys, F.text.in_(["My Keys", "\U0001F511 \u041c\u043e\u0438 \u043a\u043b\u044e\u0447\u0438"]))
    dp.message.register(menu_telegram_proxy, F.text.in_(["Telegram Proxy", "\u2708\ufe0f Telegram Proxy"]))

    # Soft matching for text variations
    dp.message.register(menu_servers, F.text.contains("Server"))
    dp.message.register(menu_servers, F.text.contains("\u0421\u0435\u0440\u0432\u0435\u0440"))

    dp.message.register(menu_my_subscription, F.text.contains("Subscription"))
    dp.message.register(menu_my_subscription, F.text.contains("\u043f\u043e\u0434\u043f\u0438\u0441"))

    dp.message.register(menu_buy_subscription, F.text.contains("Buy"))
    dp.message.register(menu_buy_subscription, F.text.contains("\u041a\u0443\u043f\u0438\u0442\u044c"))

    dp.message.register(menu_my_keys, F.text.contains("Keys"))
    dp.message.register(menu_my_keys, F.text.contains("\u043a\u043b\u044e\u0447"))

    dp.message.register(menu_telegram_proxy, F.text.contains("Proxy"))

    dp.callback_query.register(on_server_pick, F.data.startswith("server:"))
    dp.callback_query.register(on_buy_minutes, F.data.startswith("buy_minutes:"))
    dp.callback_query.register(on_buy_months, F.data.startswith("buy_months:"))
    dp.callback_query.register(on_buy_server, F.data.startswith("buyserver:"))
    dp.callback_query.register(on_new_key, F.data == "newkey")
    dp.callback_query.register(on_delete_key, F.data.startswith("delkey:"))
    dp.callback_query.register(on_device_pick, F.data.startswith("device:"))
    dp.callback_query.register(on_tgproxy_pick, F.data.startswith("tgproxy:"))
    dp.callback_query.register(on_disabled_server, F.data.startswith("disabled:"))

    # Must be last: catches unknown callback / text.
    dp.callback_query.register(on_unknown_callback)
    dp.message.register(on_unknown_message)
    return dp

def start_sub_server_in_background() -> None:
    def _runner() -> None:
        try:
            run_sub_server()
        except Exception as exc:
            print(f"Sub server failed: {exc}")

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()


async def on_startup(bot: Bot) -> None:
    asyncio.create_task(subscription_watcher(bot))


def main() -> None:
    cfg = load_config()
    token = get_bot_token(cfg)
    bot = Bot(token=token)
    dp = build_dispatcher()
    dp.startup.register(on_startup)
    start_sub_server_in_background()
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
































