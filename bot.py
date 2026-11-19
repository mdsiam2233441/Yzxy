"""All-in-One OTT Notification Bot (aiogram 3) - fully admin controlled.

Run:  pip install -r requirements.txt  ->  copy .env.example to .env  ->  python bot.py
"""
import asyncio
import html
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ottbot")

# ───────────────────────── Config ─────────────────────────
TOKEN = os.environ["8535271767:AAEk8u7aEZ9MBLyWtE2uvPKQ9nj_U0itAuA"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "1686274364").split(",") if x.strip()}
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/your_support_group")
UPDATE_URL = os.getenv("UPDATE_CHANNEL_URL", "https://t.me/your_update_channel")
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.me/your_admin_username")
PAY_INFO = os.getenv("PAY_INFO", "bKash / Nagad: 01XXXXXXXXX (Send Money)")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

OTTS = {
    "zee5": "ZEE5",
    "jiohotstar": "JioHotstar",
    "sonyliv": "Sony LIV",
    "sunnxt": "SunNXT",
    "dangalplay": "Dangal Play",
}
PLANS = [("1 Day", 1, 99), ("7 Days", 7, 299), ("30 Days", 30, 999)]  # label, days, BDT

# ───────────────────────── Database ─────────────────────────
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, joined INTEGER, banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS subs(user_id INTEGER PRIMARY KEY, expires INTEGER);
CREATE TABLE IF NOT EXISTS ott(key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS follow(user_id INTEGER, ott TEXT, PRIMARY KEY(user_id, ott));
CREATE TABLE IF NOT EXISTS seen(ott TEXT, cid TEXT, PRIMARY KEY(ott, cid));
CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, days INTEGER, used_by INTEGER);
CREATE TABLE IF NOT EXISTS accounts(ott TEXT PRIMARY KEY, username TEXT, password TEXT, active INTEGER DEFAULT 0);
"""
)
for _k in OTTS:
    db.execute("INSERT OR IGNORE INTO ott(key) VALUES(?)", (_k,))
db.commit()


def now() -> int:
    return int(time.time())


def sub_expiry(uid: int) -> int:
    r = db.execute("SELECT expires FROM subs WHERE user_id=?", (uid,)).fetchone()
    return r["expires"] if r else 0


def is_active(uid: int) -> bool:
    return sub_expiry(uid) > now()


def add_days(uid: int, days: int) -> int:
    base = max(now(), sub_expiry(uid))
    exp = base + days * 86400
    db.execute(
        "INSERT INTO subs(user_id, expires) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires",
        (uid, exp),
    )
    db.commit()
    return exp


def fmt_date(ts: int) -> str:
    return time.strftime("%d-%m-%Y %H:%M", time.localtime(ts))


def esc(s) -> str:
    return html.escape(str(s or ""))


# ───────────────────────── Buttons ─────────────────────────
def b(text: str, data: Optional[str] = None, url: Optional[str] = None, style: Optional[str] = None):
    """Inline button. style = primary | success | danger (shown as colors on newer Telegram clients).
    Emoji in the label keeps the colorful look on every client."""
    kw = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = data
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [b("💬 Support Group", url=SUPPORT_URL, style="primary"), b("📢 Update Channel", url=UPDATE_URL, style="primary")],
        [b("ℹ️ About", "m:about"), b("❓ Help", "m:help"), b("📺 OTTs", "m:otts")],
        [b("📜 Terms & Conditions", "m:terms"), b("⚙️ Settings", "m:settings")],
        [b("☎️ Contact Us", url=CONTACT_URL, style="primary"), b("💎 Plans", "m:plans", style="success")],
        [b("👤 My Plan", "m:myplan")],
    ]
    if uid in ADMIN_IDS:
        rows.append([b("🛠 Admin Panel", "a:panel", style="danger")])
    return kb(rows)


BACK = [b("« Back", "m:home")]

# ───────────────────────── OTT providers ─────────────────────────
@dataclass
class Item:
    ott: str
    cid: str  # unique id of the episode/movie on that OTT
    series: str
    season: str
    episode: str
    title: str
    lang: str
    date: str
    url: str
    image: Optional[str] = None


class Provider:
    """One class per OTT. Fill in login() and fetch_latest() for each site.

    login(username, password) -> True when the account logged in OK.
    fetch_latest() -> list[Item] of the newest releases (the bot de-duplicates by Item.cid).
    """

    key = ""

    async def login(self, username: str, password: str) -> bool:
        raise NotImplementedError

    async def fetch_latest(self) -> list[Item]:
        raise NotImplementedError


class Zee5(Provider): key = "zee5"
class JioHotstar(Provider): key = "jiohotstar"
class SonyLiv(Provider): key = "sonyliv"
class SunNxt(Provider): key = "sunnxt"
class DangalPlay(Provider): key = "dangalplay"


PROVIDERS = {p.key: p() for p in (Zee5, JioHotstar, SonyLiv, SunNxt, DangalPlay)}

# ───────────────────────── Notifications ─────────────────────────
def caption(i: Item) -> str:
    tag = OTTS[i.ott].replace(" ", "")
    return (
        f"🎬 #NewEpisode #{tag}\n\n"
        f"📺 <b>Series Name</b> : {esc(i.series)}\n"
        f"🗓 <b>Season</b> : {esc(i.season)}\n"
        f"🔢 <b>Episode</b> : {esc(i.episode)}\n"
        f"📝 <b>Title</b> : {esc(i.title)}\n"
        f"🗣 <b>Language</b> : {esc(i.lang)}\n"
        f"📅 <b>Release Date</b> : {esc(i.date)}"
    )


async def send_one(bot: Bot, uid: int, i: Item):
    markup = kb([[b("▶️ Watch Now", url=i.url, style="success")]])
    for _ in range(3):
        try:
            if i.image:
                await bot.send_photo(uid, i.image, caption=caption(i), reply_markup=markup)
            else:
                await bot.send_message(uid, caption(i), reply_markup=markup)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            db.execute("UPDATE users SET banned=1 WHERE id=?", (uid,))
            db.commit()
            return
        except Exception:
            log.exception("send failed to %s", uid)
            return


async def notify(bot: Bot, i: Item):
    rows = db.execute(
        "SELECT u.id FROM users u JOIN subs s ON s.user_id=u.id JOIN follow f ON f.user_id=u.id AND f.ott=? "
        "WHERE s.expires>? AND u.banned=0",
        (i.ott, now()),
    ).fetchall()
    for r in rows:
        await send_one(bot, r["id"], i)
        await asyncio.sleep(0.05)  # stay under Telegram flood limits


async def poll_loop(bot: Bot):
    while True:
        for key, prov in PROVIDERS.items():
            enabled = db.execute("SELECT enabled FROM ott WHERE key=?", (key,)).fetchone()
            acc = db.execute("SELECT 1 FROM accounts WHERE ott=? AND active=1", (key,)).fetchone()
            if not (enabled and enabled["enabled"] and acc):
                continue
            try:
                items = await prov.fetch_latest()
            except NotImplementedError:
                continue
            except Exception:
                log.exception("fetch failed for %s", key)
                continue
            first_run = db.execute("SELECT 1 FROM seen WHERE ott=? LIMIT 1", (key,)).fetchone() is None
            fresh = [x for x in items if not db.execute("SELECT 1 FROM seen WHERE ott=? AND cid=?", (key, x.cid)).fetchone()]
            for x in fresh:
                db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (key, x.cid))
            db.commit()
            if first_run:  # don't flood users with old content on the very first fetch
                continue
            for x in reversed(fresh):
                await notify(bot, x)
        await asyncio.sleep(POLL_SECONDS)


# ───────────────────────── User side ─────────────────────────
user = Router()

WELCOME = (
    "Hi <b>{name}</b>!\n"
    "I'm your All-in-One <b>OTT Notification Bot</b> on Telegram.\n\n"
    "I instantly notify you about the latest releases from popular OTT platforms, "
    "delivering updates within seconds ⚡ along with direct links so you never miss new content.\n\n"
    "📺 <b>Supported OTT Platforms</b>\n{otts}\n\n"
    "💡 Made with 💜 for you"
)


def otts_list() -> str:
    return "\n".join(f"• {n}" for n in OTTS.values())


def plans_text() -> str:
    lines = "\n".join(f"{n+1}. <b>{p[0]}</b> — {p[2]} BDT" for n, p in enumerate(PLANS))
    return (
        f"💎 <b>Subscription Plans</b>\n\n{lines}\n\n"
        "<b>Terms:</b>\n1. All payments are non-refundable.\n"
        "2. No compensation if the service stops functioning.\n"
        f"3. Payment: {esc(PAY_INFO)}\n\nTap <b>Buy Premium</b> to continue."
    )


async def show(c: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await c.answer()


@user.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id
    db.execute("INSERT OR IGNORE INTO users(id,name,joined) VALUES(?,?,?)", (uid, m.from_user.full_name, now()))
    db.execute("UPDATE users SET name=?, banned=0 WHERE id=?", (m.from_user.full_name, uid))
    for k in OTTS:
        db.execute("INSERT OR IGNORE INTO follow VALUES(?,?)", (uid, k))
    db.commit()
    await m.answer(WELCOME.format(name=esc(m.from_user.full_name), otts=otts_list()), reply_markup=home_kb(uid))


@user.callback_query(F.data.startswith("m:"))
async def menu(c: CallbackQuery):
    uid, act = c.from_user.id, c.data[2:]
    if act == "home":
        await show(c, WELCOME.format(name=esc(c.from_user.full_name), otts=otts_list()), home_kb(uid))
    elif act == "about":
        await show(c, "ℹ️ <b>About</b>\n\nAn all-in-one OTT release notifier. Premium members get instant alerts with direct links.", kb([BACK]))
    elif act == "help":
        await show(c, "❓ <b>Help</b>\n\n/start – menu\n/plans – plans\n/myplan – your plan\n/redeem CODE – activate a code\n/settings – choose OTTs", kb([BACK]))
    elif act == "otts":
        await show(c, f"📺 <b>Available OTTs</b>\n\n{otts_list()}", kb([BACK]))
    elif act == "terms":
        await show(c, plans_text().split("Tap")[0], kb([BACK]))
    elif act == "plans":
        await show(c, plans_text(), kb([[b("🛒 Buy Premium", "m:buy", style="success")], BACK]))
    elif act == "buy":
        rows = [[b(f"» {l} : {p} BDT", f"m:plan:{d}", style="success")] for l, d, p in PLANS]
        await show(c, "Please select your desired plan duration:", kb(rows + [[b("« Back", "m:plans")]]))
    elif act.startswith("plan:"):
        days = int(act.split(":")[1])
        label, _, price = next(p for p in PLANS if p[1] == days)
        text = (
            f"✅ <b>{label}</b> — {price} BDT\n\nPay via: {esc(PAY_INFO)}\n"
            f"Then send the payment screenshot and your ID <code>{uid}</code> to the admin."
        )
        await show(c, text, kb([[b("☎️ Contact Admin", url=CONTACT_URL, style="primary")], [b("« Back", "m:buy")]]))
    elif act == "myplan":
        exp = sub_expiry(uid)
        text = f"💎 <b>Premium active</b>\nExpires: {fmt_date(exp)}" if exp > now() else "❌ No active plan. Tap Plans to subscribe."
        await show(c, text, kb([BACK]))
    elif act == "settings":
        await show(c, "⚙️ <b>Settings</b>\nChoose which OTTs you want alerts from:", settings_kb(uid))


def settings_kb(uid: int) -> InlineKeyboardMarkup:
    on = {r["ott"] for r in db.execute("SELECT ott FROM follow WHERE user_id=?", (uid,))}
    rows = [[b(("🟢 " if k in on else "⚪ ") + n, f"f:{k}")] for k, n in OTTS.items()]
    return kb(rows + [BACK])


@user.callback_query(F.data.startswith("f:"))
async def toggle_follow(c: CallbackQuery):
    k, uid = c.data[2:], c.from_user.id
    if k in OTTS:
        if db.execute("SELECT 1 FROM follow WHERE user_id=? AND ott=?", (uid, k)).fetchone():
            db.execute("DELETE FROM follow WHERE user_id=? AND ott=?", (uid, k))
        else:
            db.execute("INSERT INTO follow VALUES(?,?)", (uid, k))
        db.commit()
    await c.message.edit_reply_markup(reply_markup=settings_kb(uid))
    await c.answer()


@user.message(Command("plans"))
async def cmd_plans(m: Message):
    await m.answer(plans_text(), reply_markup=kb([[b("🛒 Buy Premium", "m:buy", style="success")]]))


@user.message(Command("getpremium"))
async def cmd_getpremium(m: Message):
    rows = [[b(f"» {l} : {p} BDT", f"m:plan:{d}", style="success")] for l, d, p in PLANS]
    await m.answer("Please select your desired plan duration:", reply_markup=kb(rows))


@user.message(Command("myplan"))
async def cmd_myplan(m: Message):
    exp = sub_expiry(m.from_user.id)
    await m.answer(f"💎 Premium active until {fmt_date(exp)}" if exp > now() else "❌ No active plan. Use /plans")


@user.message(Command("otts"))
async def cmd_otts(m: Message):
    await m.answer(f"📺 <b>Available OTTs</b>\n\n{otts_list()}")


@user.message(Command("settings"))
async def cmd_settings(m: Message):
    await m.answer("⚙️ <b>Settings</b>\nChoose which OTTs you want alerts from:", reply_markup=settings_kb(m.from_user.id))


@user.message(Command("redeem"))
async def cmd_redeem(m: Message):
    parts = (m.text or "").split()
    if len(parts) != 2:
        return await m.answer("Usage: /redeem YOURCODE")
    r = db.execute("SELECT * FROM codes WHERE code=? AND used_by IS NULL", (parts[1].strip(),)).fetchone()
    if not r:
        return await m.answer("❌ Invalid or already used code.")
    db.execute("UPDATE codes SET used_by=? WHERE code=?", (m.from_user.id, r["code"]))
    exp = add_days(m.from_user.id, r["days"])
    await m.answer(f"🎉 Premium activated for {r['days']} day(s)!\nExpires: {fmt_date(exp)}")


# ───────────────────────── Admin side ─────────────────────────
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AdminS(StatesGroup):
    waiting = State()


def panel_kb() -> InlineKeyboardMarkup:
    return kb(
        [
            [b("➕ Grant Plan", "a:grant", style="success"), b("➖ Revoke Plan", "a:revoke", style="danger")],
            [b("🎟 Generate Codes", "a:codes", style="primary"), b("📢 Broadcast", "a:bc", style="primary")],
            [b("📺 OTTs & Logins", "a:otts", style="success"), b("📊 Stats", "a:stats")],
            [b("🧪 Test Notification", "a:test"), b("« Home", "m:home")],
        ]
    )


PROMPTS = {
    "grant": "Send: <code>USER_ID DAYS</code>  (example: <code>123456789 30</code>)",
    "revoke": "Send the <code>USER_ID</code> to revoke.",
    "codes": "Send: <code>DAYS COUNT</code>  (example: <code>7 5</code> = five 7-day codes)",
    "bc": "Send the message (text, photo, anything) to broadcast to all users. /cancel to stop.",
}


def otts_admin_kb() -> InlineKeyboardMarkup:
    rows = []
    for k, n in OTTS.items():
        en = db.execute("SELECT enabled FROM ott WHERE key=?", (k,)).fetchone()["enabled"]
        acc = db.execute("SELECT active FROM accounts WHERE ott=?", (k,)).fetchone()
        rows.append(
            [
                b(("✅ " if en else "⛔ ") + n, f"a:tog:{k}", style="success" if en else "danger"),
                b(("🔓 Logged in" if acc and acc["active"] else "🔑 Login"), f"a:login:{k}", style="primary"),
            ]
        )
    return kb(rows + [[b("« Panel", "a:panel")]])


@admin.message(Command("admin"))
async def cmd_admin(m: Message):
    await m.answer("🛠 <b>Admin Panel</b>", reply_markup=panel_kb())


@admin.callback_query(F.data.startswith("a:"))
async def admin_cb(c: CallbackQuery, state: FSMContext):
    act = c.data[2:]
    if act == "panel":
        await state.clear()
        await show(c, "🛠 <b>Admin Panel</b>", panel_kb())
    elif act in PROMPTS:
        await state.set_state(AdminS.waiting)
        await state.update_data(kind=act)
        await c.message.answer(PROMPTS[act])
        await c.answer()
    elif act == "stats":
        users = db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = db.execute("SELECT COUNT(*) n FROM subs WHERE expires>?", (now(),)).fetchone()["n"]
        seen = "\n".join(
            f"• {n}: {db.execute('SELECT COUNT(*) n FROM seen WHERE ott=?', (k,)).fetchone()['n']} tracked"
            for k, n in OTTS.items()
        )
        await show(c, f"📊 <b>Stats</b>\n\nUsers: {users}\nActive premium: {active}\n\n{seen}", kb([[b("« Panel", "a:panel")]]))
    elif act == "otts":
        await show(
            c,
            "📺 <b>OTTs & Logins</b>\n✅/⛔ turns an OTT on/off. Set credentials with:\n"
            "<code>/setlogin zee5 username password</code>\nthen tap 🔑 Login.",
            otts_admin_kb(),
        )
    elif act.startswith("tog:"):
        k = act[4:]
        db.execute("UPDATE ott SET enabled=1-enabled WHERE key=?", (k,))
        db.commit()
        await c.message.edit_reply_markup(reply_markup=otts_admin_kb())
        await c.answer("Updated")
    elif act.startswith("login:"):
        k = act[6:]
        acc = db.execute("SELECT * FROM accounts WHERE ott=?", (k,)).fetchone()
        if not acc:
            return await c.answer(f"Set credentials first: /setlogin {k} user pass", show_alert=True)
        try:
            ok = await PROVIDERS[k].login(acc["username"], acc["password"])
        except NotImplementedError:
            return await c.answer("Provider code for this OTT is not written yet (see bot.py).", show_alert=True)
        except Exception as e:
            log.exception("login failed")
            return await c.answer(f"Login error: {e}"[:190], show_alert=True)
        db.execute("UPDATE accounts SET active=? WHERE ott=?", (1 if ok else 0, k))
        db.commit()
        await c.message.edit_reply_markup(reply_markup=otts_admin_kb())
        await c.answer("✅ Logged in — notifications active" if ok else "❌ Login failed", show_alert=True)
    elif act == "test":
        sample = Item("zee5", "test", "Sample Series", "Season 1", "101", "Sample Series - Test Episode", "Hindi", time.strftime("%d-%m-%Y"), CONTACT_URL)
        await send_one(c.bot, c.from_user.id, sample)
        await c.answer("Sent")


@admin.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Cancelled.")


@admin.message(Command("setlogin"))
async def setlogin(m: Message):
    parts = (m.text or "").split(maxsplit=3)
    if len(parts) != 4 or parts[1] not in OTTS:
        return await m.answer(f"Usage: /setlogin OTT username password\nOTT = {', '.join(OTTS)}")
    db.execute(
        "INSERT INTO accounts(ott,username,password,active) VALUES(?,?,?,0) "
        "ON CONFLICT(ott) DO UPDATE SET username=excluded.username, password=excluded.password, active=0",
        (parts[1], parts[2], parts[3]),
    )
    db.commit()
    try:
        await m.delete()  # remove the password from the chat
    except Exception:
        pass
    await m.answer(f"🔑 Saved login for {OTTS[parts[1]]}. Open /admin → OTTs & Logins → Login.")


@admin.message(AdminS.waiting)
async def admin_input(m: Message, state: FSMContext):
    kind = (await state.get_data()).get("kind")
    await state.clear()
    t = (m.text or "").strip()
    try:
        if kind == "bc":
            ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned=0")]
            sent = 0
            for uid in ids:
                try:
                    await m.bot.copy_message(uid, m.chat.id, m.message_id)
                    sent += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            return await m.answer(f"📢 Sent to {sent}/{len(ids)} users.")
        if kind == "grant":
            uid, days = t.split()
            exp = add_days(int(uid), int(days))
            await m.answer(f"✅ {uid} → premium until {fmt_date(exp)}")
            try:
                await m.bot.send_message(int(uid), f"🎉 Premium activated for {days} day(s)!\nExpires: {fmt_date(exp)}")
            except Exception:
                pass
        elif kind == "revoke":
            db.execute("DELETE FROM subs WHERE user_id=?", (int(t),))
            db.commit()
            await m.answer(f"➖ Revoked plan for {t}")
        elif kind == "codes":
            days, count = (int(x) for x in t.split())
            codes = [secrets.token_hex(4).upper() for _ in range(min(count, 100))]
            db.executemany("INSERT INTO codes(code,days) VALUES(?,?)", [(c, days) for c in codes])
            db.commit()
            await m.answer(f"🎟 {days}-day codes:\n" + "\n".join(f"<code>{c}</code>" for c in codes))
    except (ValueError, TypeError):
        await m.answer("❌ Wrong format. Open /admin and try again.")


# ───────────────────────── Main ─────────────────────────
async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)  # admin first so its flows win
    dp.include_router(user)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Check alive status"),
            BotCommand(command="getpremium", description="Activate Premium Plan"),
            BotCommand(command="otts", description="Available OTTs"),
            BotCommand(command="plans", description="See available plans"),
            BotCommand(command="redeem", description="Activate plan with redeem code"),
            BotCommand(command="myplan", description="See your plan"),
            BotCommand(command="settings", description="Choose OTTs for alerts"),
        ]
    )
    poller = asyncio.create_task(poll_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        poller.cancel()


if __name__ == "__main__":
    asyncio.run(main())
