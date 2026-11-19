#!/usr/bin/env python3
"""All-in-One OTT Notification Bot - fully admin controlled.

NO pip install needed (Python 3.8+ only).
1) Copy .env.example to .env and put your BOT_TOKEN + ADMIN_IDS
2) python bot.py
"""
import copy
import html
import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("ottbot")


# ───────────────────────── Config (.env) ─────────────────────────
def load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


load_env()
TOKEN = os.getenv("BOT_TOKEN", "8535271767:AAEy7Vy1pwIwm8YZDG1uBOo1OYAPRPxeQCU").strip()
if ":" not in TOKEN:
    sys.exit(
        "\n❌ BOT_TOKEN পাওয়া যায়নি বা ভুল।\n"
        "   bot.py যে ফোল্ডারে আছে সেখানে '.env' নামে ফাইল বানান (.env.example নয়) এবং লিখুন:\n"
        "   BOT_TOKEN=123456789:AAH-your-token\n"
    )
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "1686274364").replace(" ", "1686274364").split(",") if x.strip().isdigit()}
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/updatechannle1")
UPDATE_URL = os.getenv("UPDATE_CHANNEL_URL", "https://t.me/updatechannle1")
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.me/skrbd223311")
PAY_INFO = os.getenv("PAY_INFO", "bKash / Nagad: 01XXXXXXXXX (Send Money)")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
USE_STYLE = os.getenv("BUTTON_STYLE", "1") == "1"  # colored buttons on supporting clients
API = os.getenv("API_BASE", "https://api.telegram.org").rstrip("/") + f"/bot{TOKEN}/"
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "bot.db"))

OTTS = {
    "zee5": "ZEE5",
    "jiohotstar": "JioHotstar",
    "sonyliv": "Sony LIV",
    "sunnxt": "SunNXT",
    "dangalplay": "Dangal Play",
}
PLANS = [("1 Day", 1, 99), ("7 Days", 7, 299), ("30 Days", 30, 999)]  # label, days, BDT

# ───────────────────────── Database ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
dblock = threading.RLock()
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


def run(sql, args=()):
    with dblock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.fetchall()


def one(sql, args=()):
    r = run(sql, args)
    return r[0] if r else None


def now():
    return int(time.time())


def sub_expiry(uid):
    r = one("SELECT expires FROM subs WHERE user_id=?", (uid,))
    return r["expires"] if r else 0


def add_days(uid, days):
    exp = max(now(), sub_expiry(uid)) + days * 86400
    run(
        "INSERT INTO subs(user_id, expires) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires",
        (uid, exp),
    )
    return exp


def fmt_date(ts):
    return time.strftime("%d-%m-%Y %H:%M", time.localtime(ts))


def esc(s):
    return html.escape(str(s or ""))


# ───────────────────────── Telegram API (plain HTTP) ─────────────────────────
class TgError(Exception):
    def __init__(self, desc, code=0, retry=0):
        super().__init__(desc)
        self.desc, self.code, self.retry = str(desc), code, retry


def call(method, _timeout=40, **params):
    params = {k: v for k, v in params.items() if v is not None}
    req = urllib.request.Request(
        API + method, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"ok": False, "description": str(e), "error_code": e.code}
    if not body.get("ok"):
        raise TgError(body.get("description", "error"), body.get("error_code", 0), (body.get("parameters") or {}).get("retry_after", 0))
    return body["result"]


def strip_style(markup):
    m = copy.deepcopy(markup)
    for row in m.get("inline_keyboard", []):
        for bt in row:
            bt.pop("style", None)
    return m


def api(method, **p):
    """call() + automatic retry on flood-wait + retry without button colors if Telegram rejects them."""
    for _ in range(3):
        try:
            return call(method, **p)
        except TgError as e:
            if e.retry:
                time.sleep(e.retry + 1)
            elif e.code == 400 and p.get("reply_markup") and '"style"' in json.dumps(p["reply_markup"]) and "not modified" not in e.desc:
                p["reply_markup"] = strip_style(p["reply_markup"])
            else:
                raise
    raise TgError("too many retries")


def btn(text, data=None, url=None, style=None):
    d = {"text": text}
    if url:
        d["url"] = url
    else:
        d["callback_data"] = data
    if style and USE_STYLE:
        d["style"] = style  # primary | success | danger
    return d


def kb(rows):
    return {"inline_keyboard": rows}


def send(chat, text, markup=None):
    return api("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


def edit(chat, mid, text, markup=None):
    try:
        api("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup or kb([]), disable_web_page_preview=True)
    except TgError as e:
        if "not modified" in e.desc:
            return
        send(chat, text, markup)


def answer(cb_id, text=None, alert=False):
    try:
        call("answerCallbackQuery", callback_query_id=cb_id, text=text, show_alert=alert or None)
    except Exception:
        pass


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

    login(username, password) -> True when the login worked.
    fetch_latest() -> list[Item] with the newest releases (duplicates are skipped by Item.cid).
    """

    key = ""

    def login(self, username, password):
        raise NotImplementedError

    def fetch_latest(self):
        raise NotImplementedError


class Zee5(Provider): key = "zee5"
class JioHotstar(Provider): key = "jiohotstar"
class SonyLiv(Provider): key = "sonyliv"
class SunNxt(Provider): key = "sunnxt"
class DangalPlay(Provider): key = "dangalplay"


PROVIDERS = {p.key: p() for p in (Zee5, JioHotstar, SonyLiv, SunNxt, DangalPlay)}


# ───────────────────────── Notifications ─────────────────────────
def caption(i):
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


def send_item(uid, i):
    markup = kb([[btn("▶️ Watch Now", url=i.url, style="success")]])
    try:
        if i.image:
            try:
                return api("sendPhoto", chat_id=uid, photo=i.image, caption=caption(i), parse_mode="HTML", reply_markup=markup)
            except TgError as e:
                if e.code == 403:
                    raise
        send(uid, caption(i), markup)
    except TgError as e:
        if e.code == 403:
            run("UPDATE users SET banned=1 WHERE id=?", (uid,))
        else:
            log.warning("send to %s failed: %s", uid, e)


def notify(i):
    rows = run(
        "SELECT u.id FROM users u JOIN subs s ON s.user_id=u.id JOIN follow f ON f.user_id=u.id AND f.ott=? "
        "WHERE s.expires>? AND u.banned=0",
        (i.ott, now()),
    )
    for r in rows:
        send_item(r["id"], i)
        time.sleep(0.05)  # stay under Telegram flood limits


def poll_loop():
    while True:
        for key, prov in PROVIDERS.items():
            en = one("SELECT enabled FROM ott WHERE key=?", (key,))
            acc = one("SELECT 1 FROM accounts WHERE ott=? AND active=1", (key,))
            if not (en and en["enabled"] and acc):
                continue
            try:
                items = prov.fetch_latest()
            except NotImplementedError:
                continue
            except Exception:
                log.exception("fetch failed for %s", key)
                continue
            first_run = one("SELECT 1 FROM seen WHERE ott=? LIMIT 1", (key,)) is None
            fresh = [x for x in items if not one("SELECT 1 FROM seen WHERE ott=? AND cid=?", (key, x.cid))]
            for x in fresh:
                run("INSERT OR IGNORE INTO seen VALUES(?,?)", (key, x.cid))
            if first_run:  # don't flood users with old content on the first fetch
                continue
            for x in reversed(fresh):
                notify(x)
        time.sleep(POLL_SECONDS)


# ───────────────────────── Texts & keyboards ─────────────────────────
def otts_list():
    return "\n".join(f"• {n}" for n in OTTS.values())


def welcome(name):
    return (
        f"Hi <b>{esc(name)}</b>!\n"
        "I'm your All-in-One <b>OTT Notification Bot</b> on Telegram.\n\n"
        "I instantly notify you about the latest releases from popular OTT platforms, "
        "delivering updates within seconds ⚡ along with direct links so you never miss new content.\n\n"
        f"📺 <b>Supported OTT Platforms</b>\n{otts_list()}\n\n"
        "💡 Made with 💜 for you"
    )


TERMS = (
    "📜 <b>Terms & Conditions</b>\n\n"
    "1. All payments are non-refundable.\n"
    "2. No compensation is provided if the service stops functioning.\n"
    f"3. Payment methods: {esc(PAY_INFO)}"
)


def plans_text():
    lines = "\n".join(f"{n + 1}. <b>{p[0]}</b> — {p[2]} BDT" for n, p in enumerate(PLANS))
    return f"💎 <b>Subscription Plans</b>\n\n{lines}\n\n{TERMS}\n\nTap <b>Buy Premium</b> to continue."


BACK = [btn("« Back", "m:home")]


def home_kb(uid):
    rows = [
        [btn("💬 Support Group", url=SUPPORT_URL, style="primary"), btn("📢 Update Channel", url=UPDATE_URL, style="primary")],
        [btn("ℹ️ About", "m:about"), btn("❓ Help", "m:help"), btn("📺 OTTs", "m:otts")],
        [btn("📜 Terms & Conditions", "m:terms"), btn("⚙️ Settings", "m:settings")],
        [btn("☎️ Contact Us", url=CONTACT_URL, style="primary"), btn("💎 Plans", "m:plans", style="success")],
        [btn("👤 My Plan", "m:myplan")],
    ]
    if uid in ADMIN_IDS:
        rows.append([btn("🛠 Admin Panel", "a:panel", style="danger")])
    return kb(rows)


def buy_kb():
    return kb([[btn(f"» {l} : {p} BDT", f"m:plan:{d}", style="success")] for l, d, p in PLANS] + [[btn("« Back", "m:plans")]])


def settings_kb(uid):
    on = {r["ott"] for r in run("SELECT ott FROM follow WHERE user_id=?", (uid,))}
    return kb([[btn(("🟢 " if k in on else "⚪ ") + n, f"f:{k}")] for k, n in OTTS.items()] + [BACK])


def myplan_text(uid):
    exp = sub_expiry(uid)
    return f"💎 <b>Premium active</b>\nExpires: {fmt_date(exp)}" if exp > now() else "❌ No active plan. Tap Plans to subscribe."


def panel_kb():
    return kb(
        [
            [btn("➕ Grant Plan", "a:grant", style="success"), btn("➖ Revoke Plan", "a:revoke", style="danger")],
            [btn("🎟 Generate Codes", "a:codes", style="primary"), btn("📢 Broadcast", "a:bc", style="primary")],
            [btn("📺 OTTs & Logins", "a:otts", style="success"), btn("📊 Stats", "a:stats")],
            [btn("🧪 Test Notification", "a:test"), btn("« Home", "m:home")],
        ]
    )


def otts_admin_kb():
    rows = []
    for k, n in OTTS.items():
        en = one("SELECT enabled FROM ott WHERE key=?", (k,))["enabled"]
        acc = one("SELECT active FROM accounts WHERE ott=?", (k,))
        rows.append(
            [
                btn(("✅ " if en else "⛔ ") + n, f"a:tog:{k}", style="success" if en else "danger"),
                btn("🔓 Logged in" if acc and acc["active"] else "🔑 Login", f"a:login:{k}", style="primary"),
            ]
        )
    return kb(rows + [[btn("« Panel", "a:panel")]])


PROMPTS = {
    "grant": "Send: <code>USER_ID DAYS</code>  (example: <code>123456789 30</code>)",
    "revoke": "Send the <code>USER_ID</code> to revoke.",
    "codes": "Send: <code>DAYS COUNT</code>  (example: <code>7 5</code> = five 7-day codes)",
    "bc": "Send the message (text, photo, anything) to broadcast to all users. /cancel to stop.",
}
pending = {}  # admin_id -> kind of input we are waiting for

# ───────────────────────── Handlers ─────────────────────────
def full_name(u):
    return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "there"


def register(u):
    uid = u["id"]
    is_new = one("SELECT 1 FROM users WHERE id=?", (uid,)) is None
    run("INSERT OR IGNORE INTO users(id,name,joined) VALUES(?,?,?)", (uid, full_name(u), now()))
    run("UPDATE users SET name=?, banned=0 WHERE id=?", (full_name(u), uid))
    if is_new:  # new users follow every OTT by default; later choices in Settings are kept
        for k in OTTS:
            run("INSERT OR IGNORE INTO follow VALUES(?,?)", (uid, k))


def broadcast(admin_chat, from_chat, mid):
    ids = [r["id"] for r in run("SELECT id FROM users WHERE banned=0")]
    sent = 0
    for uid in ids:
        try:
            api("copyMessage", chat_id=uid, from_chat_id=from_chat, message_id=mid)
            sent += 1
        except TgError:
            pass
        time.sleep(0.05)
    send(admin_chat, f"📢 Sent to {sent}/{len(ids)} users.")


def admin_input(m, kind):
    chat, text = m["chat"]["id"], (m.get("text") or "").strip()
    if kind == "bc":
        threading.Thread(target=broadcast, args=(chat, chat, m["message_id"]), daemon=True).start()
        return send(chat, "📢 Broadcast started…")
    try:
        if kind == "grant":
            uid, days = text.split()
            exp = add_days(int(uid), int(days))
            send(chat, f"✅ {uid} → premium until {fmt_date(exp)}")
            try:
                send(int(uid), f"🎉 Premium activated for {days} day(s)!\nExpires: {fmt_date(exp)}")
            except TgError:
                pass
        elif kind == "revoke":
            run("DELETE FROM subs WHERE user_id=?", (int(text),))
            send(chat, f"➖ Revoked plan for {text}")
        elif kind == "codes":
            days, count = (int(x) for x in text.split())
            codes = [secrets.token_hex(4).upper() for _ in range(max(1, min(count, 100)))]
            for c in codes:
                run("INSERT INTO codes(code,days) VALUES(?,?)", (c, days))
            send(chat, f"🎟 {days}-day codes:\n" + "\n".join(f"<code>{c}</code>" for c in codes))
    except (ValueError, TypeError):
        send(chat, "❌ Wrong format. Open /admin and try again.")


def on_message(m):
    if m["chat"]["type"] != "private" or "from" not in m:
        return
    u, chat = m["from"], m["chat"]["id"]
    uid = u["id"]
    text = (m.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower() if text.startswith("/") else ""
    is_admin = uid in ADMIN_IDS

    if cmd == "/cancel":
        pending.pop(uid, None)
        return send(chat, "Cancelled.")
    if is_admin and uid in pending and cmd not in ("/start", "/admin"):
        return admin_input(m, pending.pop(uid))

    if cmd == "/start":
        pending.pop(uid, None)
        register(u)
        send(chat, welcome(full_name(u)), home_kb(uid))
    elif cmd == "/plans":
        send(chat, plans_text(), kb([[btn("🛒 Buy Premium", "m:buy", style="success")]]))
    elif cmd == "/getpremium":
        send(chat, "Please select your desired plan duration:", buy_kb())
    elif cmd == "/myplan":
        send(chat, myplan_text(uid))
    elif cmd == "/otts":
        send(chat, f"📺 <b>Available OTTs</b>\n\n{otts_list()}")
    elif cmd == "/settings":
        register(u)
        send(chat, "⚙️ <b>Settings</b>\nChoose which OTTs you want alerts from:", settings_kb(uid))
    elif cmd == "/help":
        send(chat, "❓ <b>Help</b>\n\n/start – menu\n/plans – plans\n/myplan – your plan\n/redeem CODE – activate a code\n/settings – choose OTTs")
    elif cmd == "/about":
        send(chat, "ℹ️ <b>About</b>\n\nAn all-in-one OTT release notifier. Premium members get instant alerts with direct links.")
    elif cmd == "/redeem":
        code = arg.strip()
        r = one("SELECT * FROM codes WHERE code=? AND used_by IS NULL", (code,)) if code else None
        if not r:
            return send(chat, "❌ Invalid or already used code.\nUsage: /redeem YOURCODE")
        register(u)
        run("UPDATE codes SET used_by=? WHERE code=?", (uid, r["code"]))
        exp = add_days(uid, r["days"])
        send(chat, f"🎉 Premium activated for {r['days']} day(s)!\nExpires: {fmt_date(exp)}")
    elif cmd == "/admin" and is_admin:
        pending.pop(uid, None)
        send(chat, "🛠 <b>Admin Panel</b>", panel_kb())
    elif cmd == "/setlogin" and is_admin:
        parts = text.split(maxsplit=3)
        if len(parts) != 4 or parts[1].lower() not in OTTS:
            return send(chat, f"Usage: /setlogin OTT username password\nOTT = {', '.join(OTTS)}")
        k = parts[1].lower()
        run(
            "INSERT INTO accounts(ott,username,password,active) VALUES(?,?,?,0) "
            "ON CONFLICT(ott) DO UPDATE SET username=excluded.username, password=excluded.password, active=0",
            (k, parts[2], parts[3]),
        )
        try:
            call("deleteMessage", chat_id=chat, message_id=m["message_id"])  # hide the password
        except TgError:
            pass
        send(chat, f"🔑 Saved login for {OTTS[k]}. Open /admin → OTTs & Logins → Login.")


def on_callback(c):
    uid, data, cid = c["from"]["id"], c.get("data", ""), c["id"]
    msg = c.get("message") or {}
    chat, mid = msg.get("chat", {}).get("id", uid), msg.get("message_id")

    def show(text, markup):
        edit(chat, mid, text, markup)
        answer(cid)

    if data.startswith("m:"):
        act = data[2:]
        if act == "home":
            show(welcome(full_name(c["from"])), home_kb(uid))
        elif act == "about":
            show("ℹ️ <b>About</b>\n\nAn all-in-one OTT release notifier. Premium members get instant alerts with direct links.", kb([BACK]))
        elif act == "help":
            show("❓ <b>Help</b>\n\n/start – menu\n/plans – plans\n/myplan – your plan\n/redeem CODE – activate a code\n/settings – choose OTTs", kb([BACK]))
        elif act == "otts":
            show(f"📺 <b>Available OTTs</b>\n\n{otts_list()}", kb([BACK]))
        elif act == "terms":
            show(TERMS, kb([BACK]))
        elif act == "plans":
            show(plans_text(), kb([[btn("🛒 Buy Premium", "m:buy", style="success")], BACK]))
        elif act == "buy":
            show("Please select your desired plan duration:", buy_kb())
        elif act.startswith("plan:"):
            days = int(act.split(":")[1])
            label, _, price = next(p for p in PLANS if p[1] == days)
            show(
                f"✅ <b>{label}</b> — {price} BDT\n\nPay via: {esc(PAY_INFO)}\n"
                f"Then send the payment screenshot and your ID <code>{uid}</code> to the admin.",
                kb([[btn("☎️ Contact Admin", url=CONTACT_URL, style="primary")], [btn("« Back", "m:buy")]]),
            )
        elif act == "myplan":
            show(myplan_text(uid), kb([BACK]))
        elif act == "settings":
            register(c["from"])
            show("⚙️ <b>Settings</b>\nChoose which OTTs you want alerts from:", settings_kb(uid))
        else:
            answer(cid)
    elif data.startswith("f:"):
        k = data[2:]
        if k in OTTS:
            if one("SELECT 1 FROM follow WHERE user_id=? AND ott=?", (uid, k)):
                run("DELETE FROM follow WHERE user_id=? AND ott=?", (uid, k))
            else:
                run("INSERT OR IGNORE INTO follow VALUES(?,?)", (uid, k))
        show("⚙️ <b>Settings</b>\nChoose which OTTs you want alerts from:", settings_kb(uid))
    elif data.startswith("a:"):
        if uid not in ADMIN_IDS:
            return answer(cid, "Admins only", True)
        act = data[2:]
        if act == "panel":
            pending.pop(uid, None)
            show("🛠 <b>Admin Panel</b>", panel_kb())
        elif act in PROMPTS:
            pending[uid] = act
            send(chat, PROMPTS[act])
            answer(cid)
        elif act == "stats":
            users = one("SELECT COUNT(*) n FROM users")["n"]
            active = one("SELECT COUNT(*) n FROM subs WHERE expires>?", (now(),))["n"]
            seen = "\n".join(f"• {n}: {one('SELECT COUNT(*) n FROM seen WHERE ott=?', (k,))['n']} tracked" for k, n in OTTS.items())
            show(f"📊 <b>Stats</b>\n\nUsers: {users}\nActive premium: {active}\n\n{seen}", kb([[btn("« Panel", "a:panel")]]))
        elif act == "otts":
            show(
                "📺 <b>OTTs & Logins</b>\n✅/⛔ turns an OTT on/off. Set credentials with:\n"
                "<code>/setlogin zee5 username password</code>\nthen tap 🔑 Login.",
                otts_admin_kb(),
            )
        elif act.startswith("tog:"):
            run("UPDATE ott SET enabled=1-enabled WHERE key=?", (act[4:],))
            show("📺 <b>OTTs & Logins</b>", otts_admin_kb())
        elif act.startswith("login:"):
            k = act[6:]
            acc = one("SELECT * FROM accounts WHERE ott=?", (k,))
            if not acc:
                return answer(cid, f"Set credentials first:\n/setlogin {k} username password", True)
            try:
                ok = PROVIDERS[k].login(acc["username"], acc["password"])
            except NotImplementedError:
                return answer(cid, "Login code for this OTT is not written yet (see class in bot.py).", True)
            except Exception as e:
                log.exception("login failed")
                return answer(cid, f"Login error: {e}"[:190], True)
            run("UPDATE accounts SET active=? WHERE ott=?", (1 if ok else 0, k))
            edit(chat, mid, "📺 <b>OTTs & Logins</b>", otts_admin_kb())
            answer(cid, "✅ Logged in — notifications active" if ok else "❌ Login failed", True)
        elif act == "test":
            sample = Item("zee5", "test", "Sample Series", "Season 1", "101", "Sample Series - Test Episode", "Hindi", time.strftime("%d-%m-%Y"), CONTACT_URL)
            send_item(uid, sample)
            answer(cid, "Sent")
        else:
            answer(cid)
    else:
        answer(cid)


# ───────────────────────── Main loop ─────────────────────────
def main():
    try:
        me = call("getMe")
    except TgError as e:
        sys.exit(f"\n❌ Telegram টোকেন নিল না: {e.desc}\n   BotFather থেকে সঠিক টোকেন কপি করে .env এ বসান (কোনো স্পেস বা কোটেশন ছাড়া)।\n")
    except Exception as e:
        sys.exit(f"\n❌ Telegram এর সাথে কানেক্ট করা যাচ্ছে না: {e}\n   ইন্টারনেট/VPN চেক করুন।\n")
    try:
        call("deleteWebhook")
        call(
            "setMyCommands",
            commands=[
                {"command": "start", "description": "Check alive status"},
                {"command": "getpremium", "description": "Activate Premium Plan"},
                {"command": "otts", "description": "Available OTTs"},
                {"command": "plans", "description": "See available plans"},
                {"command": "redeem", "description": "Activate plan with redeem code"},
                {"command": "myplan", "description": "See your plan"},
                {"command": "settings", "description": "Choose OTTs for alerts"},
                {"command": "help", "description": "How to use"},
            ],
        )
    except TgError as e:
        log.warning("setup warning: %s", e)
    print(f"✅ Bot @{me.get('username')} is running. Admins: {sorted(ADMIN_IDS) or 'NONE - set ADMIN_IDS in .env!'}")
    threading.Thread(target=poll_loop, daemon=True).start()

    offset = 0
    while True:
        try:
            updates = call("getUpdates", _timeout=45, offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
        except TgError as e:
            if e.code == 409:
                print("⚠️ এই বট আরেক জায়গায় চলছে (অন্য টার্মিনাল/সার্ভার)। সেটা বন্ধ করুন।")
            elif e.code == 401:
                sys.exit("❌ টোকেন বাতিল হয়ে গেছে। BotFather থেকে নতুন টোকেন নিন।")
            else:
                log.warning("getUpdates: %s", e)
            time.sleep(5)
            continue
        except Exception as e:
            log.warning("network problem: %s", e)
            time.sleep(5)
            continue
        for up in updates:
            offset = up["update_id"] + 1
            try:
                if "message" in up:
                    on_message(up["message"])
                elif "callback_query" in up:
                    on_callback(up["callback_query"])
            except Exception:
                log.exception("handler crashed")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
