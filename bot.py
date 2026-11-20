#!/usr/bin/env python3
"""All-in-One OTT Notification Bot - fully admin controlled.

NO pip install needed (Python 3.8+ only).
1) Copy .env.example to .env and put your BOT_TOKEN + ADMIN_IDS
2) python bot.py
"""
import copy
import gzip
import html
import http.cookiejar
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

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
TOKEN = os.getenv("BOT_TOKEN", "8535271767:AAFvLDaC-jdr860ra6LGl6f-TCjs0EnSvmI").strip()
if ":" not in TOKEN:
    sys.exit(
        "\n❌ BOT_TOKEN পাওয়া যায়নি বা ভুল।\n"
        "   bot.py যে ফোল্ডারে আছে সেখানে '.env' নামে ফাইল বানান (.env.example নয়) এবং লিখুন:\n"
        "   BOT_TOKEN=123456789:AAH-your-token\n"
    )
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "1686274364").replace(" ", "").split(",") if x.strip().isdigit()}
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/updatechannle1")
UPDATE_URL = os.getenv("UPDATE_CHANNEL_URL", "https://t.me/updatechannle1")
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.me/skrbd223311")
PAY_INFO = os.getenv("PAY_INFO", "bKash / Nagad: 01623121880 (Send Money)")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
USE_STYLE = os.getenv("BUTTON_STYLE", "1") == "1"  # colored buttons on supporting clients
API = os.getenv("API_BASE", "https://api.telegram.org").rstrip("/") + f"/bot{TOKEN}/"
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "bot.db"))
TZ_HOURS = int(os.getenv("TZ_HOURS", "6"))  # 6 = Bangladesh time
TZ = timezone(timedelta(hours=TZ_HOURS))
TZ_NAME = os.getenv("TZ_NAME", "BDT")
BOT_NAME = os.getenv("BOT_NAME", "OTT Notification Bot")
BOT_VERSION = os.getenv("BOT_VERSION", "v1.0.0")
OWNER_NAME = os.getenv("OWNER_NAME", "Admin")
ORIGIN = os.getenv("ORIGIN", "Bangladesh")
DEVELOPER = os.getenv("DEVELOPER", "@your_admin_username")
CONTACT_HANDLE = os.getenv("CONTACT_HANDLE", DEVELOPER)
LANGS = ["Hindi", "Bengali", "Tamil", "Telugu", "Malayalam", "Kannada", "Marathi", "English"]

OTTS = {
    "zee5": "ZEE5",
    "jiohotstar": "JioHotstar",
    "sonyliv": "Sony LIV",
    "sunnxt": "SunNXT",
    "dangalplay": "Dangal Play",
    "mojtv": "Moj TV",
}
DOMAINS = {  # website -> OTT key
    "hotstar.com": "jiohotstar",
    "jiohotstar.com": "jiohotstar",
    "sunnxt.com": "sunnxt",
    "zee5.com": "zee5",
    "sonyliv.com": "sonyliv",
    "dangalplay.com": "dangalplay",
    "mojapp.in": "mojtv",
}
KIND_LABEL = {"series": "TV Show", "webseries": "Web Series", "movie": "Movie"}
KIND_WORDS = {"movie": "movie", "film": "movie", "series": "series", "serial": "series", "tvshow": "series",
              "webseries": "webseries", "websries": "webseries", "web-series": "webseries"}
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
CREATE TABLE IF NOT EXISTS accounts(ott TEXT PRIMARY KEY, username TEXT, password TEXT, active INTEGER DEFAULT 0, apikey TEXT, status TEXT, checked INTEGER);
CREATE TABLE IF NOT EXISTS user_langs(user_id INTEGER, lang TEXT, PRIMARY KEY(user_id, lang));
CREATE TABLE IF NOT EXISTS tracked(id INTEGER PRIMARY KEY AUTOINCREMENT, ott TEXT, url TEXT UNIQUE, kind TEXT, title TEXT, image TEXT, lang TEXT, added INTEGER);
"""
)
for _col in ("apikey TEXT", "status TEXT", "checked INTEGER"):  # upgrade an older bot.db
    try:
        db.execute("ALTER TABLE accounts ADD COLUMN " + _col)
    except sqlite3.OperationalError:
        pass
for _k in OTTS:
    if db.execute("SELECT 1 FROM ott WHERE key=?", (_k,)).fetchone() is None:
        db.execute("INSERT INTO ott(key) VALUES(?)", (_k,))
        db.execute("INSERT OR IGNORE INTO follow(user_id, ott) SELECT id, ? FROM users", (_k,))  # existing users follow new OTTs
db.commit()


def run(sql, args=()):
    with dblock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.fetchall()


def insert_id(sql, args=()):
    with dblock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


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


# ───────────────────────── Web pages: fetch + parse ─────────────────────────
OPENERS = {}  # one cookie session per OTT (kept in memory)
AUTH_TOKENS = {}  # OTT key -> API key / access token sent with every request
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Optional password-login hooks. A hook gets (opener, username, password) and returns True/False.
# Example:  LOGIN_HANDLERS["zee5"] = my_zee5_login
# Without a hook the bot still tracks links from the public pages (no login needed for that).
LOGIN_HANDLERS = {}


def opener_for(ott):
    if ott not in OPENERS:
        OPENERS[ott] = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return OPENERS[ott]


def fetch(url, ott=None):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,hi;q=0.8,bn;q=0.7",
            "Accept-Encoding": "gzip",
        },
    )
    tok = AUTH_TOKENS.get(ott or "")
    if tok:
        hn = os.getenv("AUTH_HEADER_" + (ott or "").upper(), "Authorization")
        req.add_header(hn, f"Bearer {tok}" if hn.lower() == "authorization" else tok)
    with opener_for(ott or "x").open(req, timeout=25) as r:
        raw = r.read(4_000_000)
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def detect_ott(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    for dom, key in DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return key
    return None


def norm_url(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"


def as_str(x):
    if isinstance(x, list):
        x = x[0] if x else ""
    if isinstance(x, dict):
        x = x.get("url") or x.get("name") or ""
    return str(x or "").strip()


def to_int(x):
    m = re.search(r"\d+", str(x or ""))
    return int(m.group()) if m else None


def meta_tags(h):
    d = {}
    for tag in re.findall(r"<meta\s[^>]*>", h, re.I):
        k = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        v = re.search(r'content\s*=\s*"([^"]*)"|content\s*=\s*\'([^\']*)\'', tag, re.I)
        if k and v:
            d.setdefault(k.group(1).lower(), html.unescape(v.group(1) if v.group(1) is not None else v.group(2)))
    return d


def ld_nodes(h):
    nodes = []
    for blk in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", h, re.S | re.I):
        try:
            data = json.loads(blk.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                nodes.append(x)
                if "@graph" in x:
                    stack.append(x["@graph"])
    return nodes


def has_type(n, *types):
    t = n.get("@type")
    t = t if isinstance(t, list) else [t]
    return any(x in types for x in t)


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def date_from_text(t):
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s\-]+(\d{1,2})(?:st|nd|rd|th)?[\s,\-]+(\d{4})", t, re.I)
    if m:
        return f"{int(m.group(2)):02d}-{MONTHS[m.group(1)[:3].lower()]:02d}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?[\s\-]+(\d{4})", t, re.I)
    if m:
        return f"{int(m.group(1)):02d}-{MONTHS[m.group(2)[:3].lower()]:02d}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b", t)
    if m:
        return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}-{m.group(3)}"
    return ""


def se_from_text(t):
    """Season / episode numbers from text like 'S02 E14', 'Season 2 Episode 14', 'Episode 414', 'Ep 7'."""
    m = re.search(r"\bS(?:eason)?\s*0*(\d+)\s*[,:\-]?\s*E(?:p(?:isode)?)?\s*\.?\s*0*(\d+)", t, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    s = re.search(r"\bseason\s*0*(\d+)", t, re.I)
    e = re.search(r"\b(?:episode|ep)\s*\.?\s*0*(\d+)", t, re.I)
    return (int(s.group(1)) if s else None), (int(e.group(1)) if e else None)


def to_local(s):
    """ISO date/time text -> ('dd-mm-yyyy', 'hh:mm AM' or '') in the configured time zone."""
    s = str(s or "").strip()
    if not s:
        return "", ""
    has_time = bool(re.search(r"\d{2}:\d{2}", s))
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", s)
        if not m:
            return "", ""
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0))
    if d.tzinfo:
        d = d.astimezone(TZ)
    return d.strftime("%d-%m-%Y"), (d.strftime("%I:%M %p") if has_time else "")


def parse_page(h, url):
    """Pull title, poster image, language, type and any episodes out of a page (JSON-LD + OpenGraph)."""
    meta, nodes = meta_tags(h), ld_nodes(h)
    t = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
    p = {
        "title": as_str(meta.get("og:title") or meta.get("twitter:title")) or (html.unescape(t.group(1)).strip() if t else ""),
        "image": as_str(meta.get("og:image") or meta.get("twitter:image")),
        "lang": "",
        "kind": None,
        "date": "",
        "time": "",
        "season": None,
        "episode": None,
        "series_name": "",
        "episodes": [],
    }
    og_type = meta.get("og:type", "").lower()
    if "movie" in og_type:
        p["kind"] = "movie"
    elif "tv_show" in og_type or "episode" in og_type:
        p["kind"] = "series"

    def ep_dict(e, season=None):
        return {
            "url": as_str(e.get("url")),
            "title": as_str(e.get("name")),
            "season": to_int(e.get("seasonNumber")) or season or to_int((e.get("partOfSeason") or {}).get("seasonNumber") if isinstance(e.get("partOfSeason"), dict) else None),
            "episode": to_int(e.get("episodeNumber")),
            "date": as_str(e.get("datePublished") or e.get("uploadDate") or e.get("dateCreated")),
            "image": as_str(e.get("image") or e.get("thumbnailUrl")),
            "lang": as_str(e.get("inLanguage")),
        }

    for n in nodes:
        if has_type(n, "TVSeries", "TVShow", "CreativeWorkSeries"):
            p["kind"] = "series"
            p["title"] = as_str(n.get("name")) or p["title"]
            p["image"] = as_str(n.get("image")) or p["image"]
            p["lang"] = as_str(n.get("inLanguage")) or p["lang"]
            for e in n.get("episode") or []:
                if isinstance(e, dict):
                    p["episodes"].append(ep_dict(e))
            for se in n.get("containsSeason") or []:
                if isinstance(se, dict):
                    sn = to_int(se.get("seasonNumber"))
                    for e in se.get("episode") or []:
                        if isinstance(e, dict):
                            p["episodes"].append(ep_dict(e, sn))
        elif has_type(n, "Movie"):
            p["kind"] = p["kind"] or "movie"
            p["title"] = as_str(n.get("name")) or p["title"]
            p["image"] = as_str(n.get("image")) or p["image"]
            p["lang"] = as_str(n.get("inLanguage")) or p["lang"]
            p["date"], p["time"] = to_local(as_str(n.get("datePublished") or n.get("dateCreated")))
        elif has_type(n, "TVEpisode"):
            d = ep_dict(n)
            p["title"] = d["title"] or p["title"]
            p["image"] = d["image"] or p["image"]
            p["lang"] = d["lang"] or p["lang"]
            p["season"], p["episode"] = d["season"], d["episode"]
            p["date"], p["time"] = to_local(d["date"])
            sr = n.get("partOfSeries")
            if isinstance(sr, dict):
                p["series_name"] = as_str(sr.get("name"))
            ps = n.get("partOfSeason")
            if isinstance(ps, dict) and not p["season"]:
                p["season"] = to_int(ps.get("seasonNumber"))
        elif has_type(n, "VideoObject"):
            p["image"] = p["image"] or as_str(n.get("thumbnailUrl"))
            if not p["date"]:
                p["date"], p["time"] = to_local(as_str(n.get("uploadDate")))
            p["lang"] = p["lang"] or as_str(n.get("inLanguage"))
    if not p["lang"]:
        p["lang"] = ""
    p["image"] = urljoin(url, p["image"]) if p["image"] else ""
    for e in p["episodes"]:
        if e["url"]:
            e["url"] = urljoin(url, e["url"])
        if e["image"]:
            e["image"] = urljoin(url, e["image"])
    return p


def guess_kind(url, parsed_kind):
    u = url.lower()
    if "web-series" in u or "webseries" in u or "/webshow" in u or "web-show" in u:
        return "webseries"
    if parsed_kind:
        return parsed_kind
    if "/movie" in u or "/film" in u:
        return "movie"
    return "series"


def child_links(h, base_url):
    """Links on a series page that live under the series URL (= its episodes)."""
    bp = urlparse(base_url)
    base_path = bp.path.rstrip("/")
    out, seen_u = [], set()
    for m in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', h, re.I):
        full = urljoin(base_url, html.unescape(m.group(1)))
        q = urlparse(full)
        if q.netloc.replace("www.", "") != bp.netloc.replace("www.", ""):
            continue
        path = q.path.rstrip("/")
        if base_path and path.startswith(base_path + "/") and path != base_path:
            nu = f"{q.scheme}://{q.netloc}{path}"
            if nu not in seen_u:
                seen_u.add(nu)
                out.append(nu)
    return out


def discover(url, h, p):
    """All episode URLs currently visible on a series page (JSON-LD episodes + child links)."""
    found, seen_u = [], set()
    for e in p["episodes"]:
        if e["url"] and norm_url(e["url"]) not in seen_u:
            seen_u.add(norm_url(e["url"]))
            found.append(dict(e, url=norm_url(e["url"])))
    for u in child_links(h, url):
        if u not in seen_u:
            seen_u.add(u)
            found.append({"url": u, "title": "", "season": None, "episode": None, "date": "", "image": "", "lang": ""})
    return found


# ───────────────────────── Account login (password / API key) ─────────────────────────
def env_k(name, k):
    return os.getenv(f"{name}_{k.upper()}", "").strip()


def generic_login(k, url, user, pw):
    """POST the e-mail/username + password to LOGIN_URL_<OTT>. Cookies stay in the OTT's session;
    a token found in the JSON reply is sent as the auth header from then on."""
    tpl = env_k("LOGIN_BODY", k) or '{"email":"{username}","password":"{password}"}'
    is_json = tpl.lstrip().startswith("{")
    ju, jp = json.dumps(user)[1:-1], json.dumps(pw)[1:-1]
    body = tpl.replace("{username}", ju if is_json else urllib.parse.quote(user)).replace("{password}", jp if is_json else urllib.parse.quote(pw))
    req = urllib.request.Request(
        url, data=body.encode(), method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json" if is_json else "application/x-www-form-urlencoded", "Accept": "application/json, text/plain, */*"},
    )
    with opener_for(k).open(req, timeout=25) as r:
        text = r.read(2_000_000).decode("utf-8", "replace")
    need = env_k("LOGIN_OK", k)
    if need and need not in text:
        return "failed", "Server answered, but the success text was not found (wrong e-mail/password?)"
    try:
        data = json.loads(text)
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for key in ("token", "access_token", "accessToken", "authToken", "auth_token"):
                    if isinstance(x.get(key), str) and x[key]:
                        AUTH_TOKENS[k] = x[key]
                        return "ok", "Logged in (token received)"
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    except ValueError:
        pass
    return "ok", "Logged in"


def login_account(k):
    """Try to log in / verify the saved account of one OTT. Returns (state, message);
    state: ok | failed | unverified | none. 'ok' is only returned when the site really accepted it."""
    acc = one("SELECT * FROM accounts WHERE ott=?", (k,))
    if not acc:
        return "none", f"Nothing saved yet. Use /setlogin {k} email password  or  /setkey {k} API_KEY"
    try:
        hook = LOGIN_HANDLERS.get(k)
        if hook and acc["username"]:
            ok = hook(opener_for(k), acc["username"], acc["password"] or "")
            return ("ok", "Logged in") if ok else ("failed", "Login rejected by the site")
        url = env_k("LOGIN_URL", k)
        if url and acc["username"]:
            return generic_login(k, url, acc["username"], acc["password"] or "")
        if acc["apikey"]:
            AUTH_TOKENS[k] = acc["apikey"]
            vurl = env_k("VERIFY_URL", k)
            if vurl:
                fetch(vurl, k)  # raises on 401/403/…
                return "ok", "API key accepted by the site"
            return "unverified", f"API key saved and sent with every request, but VERIFY_URL_{k.upper()} is not set in .env so it cannot be checked."
        return "unverified", f"Saved, but no login endpoint is set. Add LOGIN_URL_{k.upper()} to .env (see README)."
    except urllib.error.HTTPError as e:
        return "failed", f"Site answered HTTP {e.code} (wrong login, or the endpoint is not correct)"
    except Exception as e:
        return "failed", str(e)[:150]


def save_status(k, state):
    run("UPDATE accounts SET active=?, status=?, checked=? WHERE ott=?", (1 if state == "ok" else 0, state, now(), k))


def restore_sessions():
    for r in run("SELECT ott, apikey FROM accounts WHERE active=1 OR apikey IS NOT NULL"):
        if r["apikey"]:
            AUTH_TOKENS[r["ott"]] = r["apikey"]
    for r in run("SELECT ott FROM accounts WHERE active=1 AND username IS NOT NULL"):
        state, _ = login_account(r["ott"])
        save_status(r["ott"], state)


# ───────────────────────── Items & tracked links ─────────────────────────
@dataclass
class Item:
    ott: str
    cid: str  # unique id of this episode/movie
    kind: str  # series | webseries | movie
    series: str
    season: str
    episode: str
    title: str
    lang: str
    date: str
    time: str
    url: str
    series_url: str = ""
    image: Optional[str] = None


LANG_CODES = {"hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "kn": "Kannada",
              "mr": "Marathi", "en": "English", "pa": "Punjabi", "gu": "Gujarati", "bho": "Bhojpuri"}


def norm_lang(s):
    s = (s or "").strip()
    if not s:
        return "N/A"
    return LANG_CODES.get(s.lower().split("-")[0], s.title() if len(s) > 3 else s.upper())


def lang_ok(uid, lang):
    sel = [x["lang"].lower() for x in run("SELECT lang FROM user_langs WHERE user_id=?", (uid,))]
    if not sel or lang in ("N/A", ""):
        return True
    return any(s in lang.lower() for s in sel)


def slug_title(url):
    last = urlparse(url).path.rstrip("/").split("/")
    parts = [x for x in last if x and not re.fullmatch(r"[0-9a-z\-]*\d[0-9a-z\-]*", x) or "-" in x]
    return (parts[-1] if parts else url).replace("-", " ").title()


def build_item(tr, ep_url, hint=None, page=None):
    """Create a notification Item for one episode (or the movie itself)."""
    hint, page = hint or {}, page or {}
    kind = tr["kind"]
    now_l = datetime.now(TZ)
    if kind == "movie":
        title = page.get("title") or tr["title"]
        d, t = page.get("date", ""), page.get("time", "")
        return Item(tr["ott"], f"{tr['id']}|{norm_url(ep_url)}", kind, tr["title"], "-", "-", title, norm_lang(tr["lang"] or page.get("lang")),
                    d or now_l.strftime("%d-%m-%Y"), t or now_l.strftime("%I:%M %p"), ep_url, "", page.get("image") or tr["image"])
    title = page.get("title") or hint.get("title") or slug_title(ep_url)
    season = page.get("season") or hint.get("season")
    episode = page.get("episode") or hint.get("episode")
    text = f"{title} {ep_url}"
    s2, e2 = se_from_text(title)
    season, episode = season or s2, episode or e2
    d, t = page.get("date", ""), page.get("time", "")
    if not d and hint.get("date"):
        d, t = to_local(hint["date"])
    d = d or date_from_text(text) or now_l.strftime("%d-%m-%Y")
    t = t or now_l.strftime("%I:%M %p")
    return Item(tr["ott"], f"{tr['id']}|{norm_url(ep_url)}", kind, tr["title"], f"Season {season}" if season else "Season 1",
                str(episode) if episode else "N/A", title, norm_lang(tr["lang"] or page.get("lang") or hint.get("lang")), d, t,
                ep_url, tr["url"], page.get("image") or hint.get("image") or tr["image"])


def seen_has(cid_key, ott):
    return one("SELECT 1 FROM seen WHERE ott=? AND cid=?", (ott, cid_key)) is not None


def mark_seen(tr, urls):
    for u in urls:
        run("INSERT OR IGNORE INTO seen VALUES(?,?)", (tr["ott"], f"{tr['id']}|{norm_url(u)}"))


def check_tracked(tr):
    """Return new episodes (as Items) of one tracked series. Movies are announced manually (Notify button)."""
    if tr["kind"] == "movie":
        return []
    h = fetch(tr["url"], tr["ott"])
    p = parse_page(h, tr["url"])
    found = discover(tr["url"], h, p)
    new = [f for f in found if not seen_has(f"{tr['id']}|{f['url']}", tr["ott"])]
    mark_seen(tr, [f["url"] for f in found])
    if any(f["date"] for f in new):
        new.sort(key=lambda f: (to_local(f["date"])[0][::-1] if f["date"] else "", f["season"] or 0, f["episode"] or 0))
    else:
        new.reverse()
    items = []
    for f in new[-5:]:  # never blast more than 5 at once
        try:
            page = parse_page(fetch(f["url"], tr["ott"]), f["url"])
        except Exception as e:
            log.warning("episode page failed %s: %s", f["url"], e)
            page = {}
        items.append(build_item(tr, f["url"], f, page))
    return items


def latest_item(tr):
    """The newest episode (series) or the movie itself - used by the 'Notify now' button."""
    h = fetch(tr["url"], tr["ott"])
    p = parse_page(h, tr["url"])
    if tr["kind"] == "movie":
        return build_item(tr, tr["url"], None, p)
    found = discover(tr["url"], h, p)
    if not found:
        return build_item(tr, tr["url"], None, p)
    dated = [f for f in found if f["date"]]
    f = max(dated, key=lambda f: (to_local(f["date"])[0][::-1], f["season"] or 0, f["episode"] or 0)) if dated else found[0]
    try:
        page = parse_page(fetch(f["url"], tr["ott"]), f["url"])
    except Exception:
        page = {}
    return build_item(tr, f["url"], f, page)


def add_link(chat, line):
    parts = line.split()
    if not parts:
        return
    url = parts[0] if parts[0].lower().startswith("http") else "https://" + parts[0]
    ott = detect_ott(url)
    if not ott:
        return send(chat, f"❌ Unsupported site: {esc(url)}\nSupported: hotstar, sunnxt, zee5, sonyliv, dangalplay, mojapp")
    override, lang_words = None, []
    for w in parts[1:]:
        if w.lower() in KIND_WORDS and not override:
            override = KIND_WORDS[w.lower()]
        else:
            lang_words.append(w)
    lang = " ".join(lang_words)
    if one("SELECT 1 FROM tracked WHERE url=?", (url,)):
        return send(chat, "ℹ️ This link is already tracked. See 📋 Tracked Links.")
    try:
        h = fetch(url, ott)
    except Exception as e:
        return send(chat, f"❌ Could not open the link: {esc(e)}\nCheck the URL (and that the site is reachable from this computer/server).")
    p = parse_page(h, url)
    kind = override or guess_kind(url, p["kind"])
    title = p["title"] or slug_title(url)
    tid = insert_id(
        "INSERT INTO tracked(ott,url,kind,title,image,lang,added) VALUES(?,?,?,?,?,?,?)",
        (ott, url, kind, title, p["image"], lang or p["lang"], now()),
    )
    tr = one("SELECT * FROM tracked WHERE id=?", (tid,))
    eps = 0
    if kind != "movie":
        found = discover(url, h, p)
        mark_seen(tr, [f["url"] for f in found])  # existing episodes = baseline, no spam
        eps = len(found)
    info = (
        f"✅ <b>Link added</b>\n\n📌 Type : {KIND_LABEL[kind]}\n🌐 Platform : {OTTS[ott]}\n📺 Name : {esc(title)}\n"
        f"🗣 Language : {esc(tr['lang'] or 'N/A')}\n🖼 Image : {'found' if p['image'] else 'not found'}\n"
        + (f"🔢 Episodes seen now : {eps} (new ones will be notified automatically)\n" if kind != "movie" else "🎞 Movies are announced with the button below.\n")
        + ("\n⚠️ No episodes could be read from this page - the site may load them with JavaScript. Use 📤 to test what gets detected." if kind != "movie" and not eps else "")
    )
    markup = kb([[btn("📤 Notify users now", f"a:push:{tid}", style="success"), btn("🗑 Remove", f"a:del:{tid}", style="danger")]])
    try:
        if p["image"]:
            api("sendPhoto", chat_id=chat, photo=p["image"], caption=info, parse_mode="HTML", reply_markup=markup)
            return
    except TgError:
        pass
    send(chat, info, markup)


def push_now(chat, tid):
    tr = one("SELECT * FROM tracked WHERE id=?", (tid,))
    if not tr:
        return send(chat, "❌ Link not found.")
    try:
        item = latest_item(tr)
    except Exception as e:
        return send(chat, f"❌ Could not read the page: {esc(e)}")
    n = notify(item)
    send(chat, f"📤 Sent to {n} subscriber(s):\n\n{caption(item)}")


# ───────────────────────── Notifications ─────────────────────────
def caption(i):
    tag = OTTS[i.ott].replace(" ", "")
    head = "#NewMovie" if i.kind == "movie" else "#NewEpisode"
    lines = [f"🎬 {head} #{tag}", "", f"📌 <b>Type</b> : {KIND_LABEL.get(i.kind, 'TV Show')}"]
    lines.append(f"📺 <b>{'Movie' if i.kind == 'movie' else 'Series'} Name</b> : {esc(i.series)}")
    if i.kind != "movie":
        lines += [f"🗓 <b>Season</b> : {esc(i.season)}", f"🔢 <b>Episode</b> : {esc(i.episode)}", f"📝 <b>Episode Name</b> : {esc(i.title)}"]
    lines += [
        f"🗣 <b>Language</b> : {esc(i.lang)}",
        f"📅 <b>Release Date</b> : {esc(i.date)}",
        f"🕒 <b>Time</b> : {esc(i.time)} ({TZ_NAME})",
        f"🌐 <b>Platform</b> : {OTTS[i.ott]}",
    ]
    return "\n".join(lines)


def item_markup(i):
    rows = [[btn("🎞 Watch Movie" if i.kind == "movie" else "▶️ Watch Episode", url=i.url, style="success")]]
    if i.series_url and norm_url(i.series_url) != norm_url(i.url):
        rows.append([btn("📺 Series Page", url=i.series_url, style="primary")])
    return kb(rows)


def send_item(uid, i):
    markup = item_markup(i)
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
    n = 0
    for r in rows:
        if not lang_ok(r["id"], i.lang):
            continue
        send_item(r["id"], i)
        n += 1
        time.sleep(0.05)  # stay under Telegram flood limits
    return n


def poll_loop():
    while True:
        for tr in run("SELECT t.* FROM tracked t JOIN ott o ON o.key=t.ott WHERE o.enabled=1"):
            try:
                for item in check_tracked(tr):
                    notify(item)
            except Exception as e:
                log.warning("check failed for %s: %s", tr["url"], e)
        time.sleep(POLL_SECONDS)


# ───────────────────────── Texts & keyboards ─────────────────────────
def otts_list():
    return "\n".join(f"• {n}" for n in OTTS.values())


def welcome(name):
    return (
        f"Hi <b>{esc(name)}</b>!\n"
        f"I'm your All-in-One <b>{esc(BOT_NAME)}</b> on Telegram.\n\n"
        "I instantly notify you about the latest releases from popular OTT platforms, "
        "delivering updates within seconds ⚡ along with direct links so you never miss new content.\n\n"
        f"📺 <b>Supported OTT Platforms</b>\n{otts_list()}\n\n"
        "💡 Made with 💜 for you"
    )


def help_text():
    return (
        "<b>Available Commands:</b>\n\n"
        "- /start - Check bot status\n"
        "- /help - Command instructions\n"
        "- /plans - View available subscription plans\n"
        "- /otts - List supported OTT platforms\n"
        "- /about - Bot information\n"
        "- /settings - Customize language settings for OTT notifications\n"
        "- /myplan - Check your current subscription plan\n"
        "- /id - Retrieve your Telegram ID\n\n"
        f"For further assistance, please contact {esc(CONTACT_HANDLE)}."
    )


def about_text():
    return (
        f"<b>{esc(BOT_NAME.upper())}</b>\n\n"
        f"- <b>Bot Name:</b> {esc(BOT_NAME)}\n"
        f"- <b>Version :</b> <code>{esc(BOT_VERSION)}</code>\n"
        "- <b>Language:</b> English\n"
        f"- <b>Owner:</b> {esc(OWNER_NAME)}\n"
        f"- <b>Origin:</b> {esc(ORIGIN)}\n"
        f"- <b>Developer:</b> {esc(DEVELOPER)}\n\n"
        "Providing you with round-the-clock updates and a user-friendly interface for all your OTT notifications."
    )


def otts_text():
    return f"📺 <b>Supported OTT Platforms</b>\n\n{otts_list()}"


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


def help_kb():
    return kb([[btn("ABOUT", "m:about", style="success"), btn("OTTS", "m:otts", style="success")],
               [btn("SUPPORT", url=SUPPORT_URL, style="primary"), btn("HOME", "m:home", style="success")]])


def about_kb():
    return kb([[btn("UPDATE CHANNEL", url=UPDATE_URL, style="primary"), btn("HELP", "m:help", style="success")],
               [btn("SUPPORT", url=SUPPORT_URL, style="primary")]])


def otts_kb():
    return kb([[btn("PLANS", "m:plans", style="success"), btn("HOME", "m:home", style="success")]])


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


SETTINGS_TEXT = "⚙️ <b>Settings</b>\nCustomize language settings and OTT alerts:"


def settings_kb():
    return kb([[btn("🗣 Languages", "m:s:langs", style="primary"), btn("📺 OTT Alerts", "m:s:otts", style="primary")], BACK])


def ott_follow_kb(uid):
    on = {r["ott"] for r in run("SELECT ott FROM follow WHERE user_id=?", (uid,))}
    return kb([[btn(("🟢 " if k in on else "⚪ ") + n, f"f:{k}")] for k, n in OTTS.items()] + [[btn("« Back", "m:settings")]])


def lang_kb(uid):
    on = {r["lang"] for r in run("SELECT lang FROM user_langs WHERE user_id=?", (uid,))}
    rows, row = [], []
    for l in LANGS:
        row.append(btn(("🟢 " if l in on else "⚪ ") + l, f"l:{l}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(rows + [[btn("♻️ All languages", "l:all", style="success")], [btn("« Back", "m:settings")]])


LANG_TEXT = "🗣 <b>Notification Languages</b>\nPick the languages you want alerts in.\nNothing selected = alerts in <b>every</b> language."


def myplan_text(uid):
    exp = sub_expiry(uid)
    return f"💎 <b>Premium active</b>\nExpires: {fmt_date(exp)}" if exp > now() else "❌ No active plan. Tap Plans to subscribe."


def panel_kb():
    return kb(
        [
            [btn("➕ Grant Plan", "a:grant", style="success"), btn("➖ Revoke Plan", "a:revoke", style="danger")],
            [btn("🎟 Generate Codes", "a:codes", style="primary"), btn("📢 Broadcast", "a:bc", style="primary")],
            [btn("🔗 Add Link", "a:link", style="success"), btn("📋 Tracked Links", "a:links", style="primary")],
            [btn("🔐 Accounts", "a:acc", style="primary"), btn("📺 OTTs On/Off", "a:otts", style="primary")],
            [btn("📊 Stats", "a:stats"), btn("🧪 Test Notification", "a:test")],
            [btn("« Home", "m:home")],
        ]
    )


def otts_admin_kb():
    rows = []
    for k, n in OTTS.items():
        en = one("SELECT enabled FROM ott WHERE key=?", (k,))["enabled"]
        rows.append([btn(("✅ " if en else "⛔ ") + n, f"a:tog:{k}", style="success" if en else "danger")])
    return kb(rows + [[btn("« Panel", "a:panel")]])


STATE_ICON = {"ok": "🟢", "failed": "🔴", "unverified": "🟡"}
STATE_TEXT = {"ok": "logged in", "failed": "login failed", "unverified": "saved, not verified"}


def acc_text():
    lines = ["🔐 <b>Account Board</b>\n"]
    for k, n in OTTS.items():
        a = one("SELECT * FROM accounts WHERE ott=?", (k,))
        if not a:
            lines.append(f"⚪ <b>{n}</b> — not set")
            continue
        who = a["username"] or ("API key" if a["apikey"] else "")
        extra = " + API key" if a["username"] and a["apikey"] else ""
        when = f" · {fmt_date(a['checked'])}" if a["checked"] else ""
        lines.append(f"{STATE_ICON.get(a['status'], '🟡')} <b>{n}</b> — {STATE_TEXT.get(a['status'], 'saved, not verified')} ({esc(who)}{extra}){when}")
    lines.append(
        "\n<b>Add / change:</b>\n<code>/setlogin zee5 email_or_username password</code>\n<code>/setkey zee5 API_KEY</code>\n"
        "OTT names: " + ", ".join(OTTS) + "\nThen tap 🔑 to verify."
    )
    return "\n".join(lines)


def acc_kb():
    rows = [[btn(f"🔑 {n}", f"a:login:{k}", style="primary"), btn("🚪 Logout", f"a:logout:{k}", style="danger")] for k, n in OTTS.items()]
    return kb(rows + [[btn("🔄 Refresh", "a:acc"), btn("« Panel", "a:panel")]])


PROMPTS = {
    "grant": "Send: <code>USER_ID DAYS</code>  (example: <code>123456789 30</code>)",
    "revoke": "Send the <code>USER_ID</code> to revoke.",
    "codes": "Send: <code>DAYS COUNT</code>  (example: <code>7 5</code> = five 7-day codes)",
    "bc": "Send the message (text, photo, anything) to broadcast to all users. /cancel to stop.",
    "link": (
        "🔗 Send one or more links (one per line) of a <b>serial / web series / movie</b> from "
        "hotstar, sunnxt, zee5, sonyliv, dangalplay or mojapp.\n\n"
        "Optional words after the link: the type (<code>movie</code>, <code>webseries</code>, <code>serial</code>) "
        "and the language.\nExample:\n<code>https://www.zee5.com/tv-shows/details/ganga-mai-ki-betiyan/0-6-4z5793364 serial Hindi</code>\n\n/cancel to stop."
    ),
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


def bg(fn, *a):
    def safe():
        try:
            fn(*a)
        except Exception:
            log.exception("background job failed")

    threading.Thread(target=safe, daemon=True).start()


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


def add_links(chat, lines):
    for line in lines:
        add_link(chat, line)


def login_job(chat, k):
    state, msg = login_account(k)
    if state != "none":
        save_status(k, state)
    send(chat, f"{STATE_ICON.get(state, '⚪')} <b>{OTTS[k]}</b>: {esc(msg)}")


def admin_input(m, kind):
    chat, text = m["chat"]["id"], (m.get("text") or "").strip()
    if kind == "bc":
        bg(broadcast, chat, chat, m["message_id"])
        return send(chat, "📢 Broadcast started…")
    if kind == "link":
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if not lines:
            return send(chat, "❌ No link found.")
        send(chat, f"⏳ Checking {len(lines)} link(s)…")
        return bg(add_links, chat, lines)
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


def hide_message(m):
    try:
        call("deleteMessage", chat_id=m["chat"]["id"], message_id=m["message_id"])  # hide passwords / keys
    except TgError:
        pass


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
        send(chat, otts_text(), otts_kb())
    elif cmd == "/settings":
        register(u)
        send(chat, SETTINGS_TEXT, settings_kb())
    elif cmd == "/help":
        send(chat, help_text(), help_kb())
    elif cmd == "/about":
        send(chat, about_text(), about_kb())
    elif cmd == "/id":
        send(chat, f"🆔 Your Telegram ID: <code>{uid}</code>")
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
    elif cmd == "/addlink" and is_admin:
        lines = [x.strip() for x in arg.splitlines() if x.strip()]
        if not lines:
            return send(chat, PROMPTS["link"])
        bg(add_links, chat, lines)
    elif cmd == "/setlogin" and is_admin:
        parts = text.split(maxsplit=3)
        if len(parts) != 4 or parts[1].lower() not in OTTS:
            return send(chat, f"Usage: /setlogin OTT email_or_username password\nOTT = {', '.join(OTTS)}")
        k = parts[1].lower()
        run(
            "INSERT INTO accounts(ott,username,password,active,status) VALUES(?,?,?,0,'saved') "
            "ON CONFLICT(ott) DO UPDATE SET username=excluded.username, password=excluded.password, active=0, status='saved'",
            (k, parts[2], parts[3]),
        )
        hide_message(m)
        send(chat, f"🔑 Saved login for {OTTS[k]}. Verifying…")
        bg(login_job, chat, k)
    elif cmd == "/setkey" and is_admin:
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or parts[1].lower() not in OTTS:
            return send(chat, f"Usage: /setkey OTT API_KEY\nOTT = {', '.join(OTTS)}")
        k = parts[1].lower()
        run(
            "INSERT INTO accounts(ott,apikey,active,status) VALUES(?,?,0,'saved') "
            "ON CONFLICT(ott) DO UPDATE SET apikey=excluded.apikey, active=0, status='saved'",
            (k, parts[2].strip()),
        )
        AUTH_TOKENS[k] = parts[2].strip()
        hide_message(m)
        send(chat, f"🗝 Saved API key for {OTTS[k]}. Verifying…")
        bg(login_job, chat, k)


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
            show(about_text(), about_kb())
        elif act == "help":
            show(help_text(), help_kb())
        elif act == "otts":
            show(otts_text(), otts_kb())
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
            show(SETTINGS_TEXT, settings_kb())
        elif act == "s:otts":
            show("📺 <b>OTT Alerts</b>\nChoose which OTTs you want alerts from:", ott_follow_kb(uid))
        elif act == "s:langs":
            show(LANG_TEXT, lang_kb(uid))
        else:
            answer(cid)
    elif data.startswith("f:"):
        k = data[2:]
        if k in OTTS:
            if one("SELECT 1 FROM follow WHERE user_id=? AND ott=?", (uid, k)):
                run("DELETE FROM follow WHERE user_id=? AND ott=?", (uid, k))
            else:
                run("INSERT OR IGNORE INTO follow VALUES(?,?)", (uid, k))
        show("📺 <b>OTT Alerts</b>\nChoose which OTTs you want alerts from:", ott_follow_kb(uid))
    elif data.startswith("l:"):
        l = data[2:]
        if l == "all":
            run("DELETE FROM user_langs WHERE user_id=?", (uid,))
        elif l in LANGS:
            if one("SELECT 1 FROM user_langs WHERE user_id=? AND lang=?", (uid, l)):
                run("DELETE FROM user_langs WHERE user_id=? AND lang=?", (uid, l))
            else:
                run("INSERT OR IGNORE INTO user_langs VALUES(?,?)", (uid, l))
        show(LANG_TEXT, lang_kb(uid))
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
            links = one("SELECT COUNT(*) n FROM tracked")["n"]
            show(f"📊 <b>Stats</b>\n\nUsers: {users}\nActive premium: {active}\nTracked links: {links}", kb([[btn("« Panel", "a:panel")]]))
        elif act == "otts":
            show("📺 <b>OTTs On/Off</b>\nTurn the automatic checking of an OTT on or off:", otts_admin_kb())
        elif act.startswith("tog:"):
            run("UPDATE ott SET enabled=1-enabled WHERE key=?", (act[4:],))
            show("📺 <b>OTTs On/Off</b>\nTurn the automatic checking of an OTT on or off:", otts_admin_kb())
        elif act == "acc":
            show(acc_text(), acc_kb())
        elif act.startswith("login:"):
            k = act[6:]
            if k not in OTTS:
                return answer(cid)
            answer(cid, "Checking…")
            bg(login_job, chat, k)
        elif act.startswith("logout:"):
            k = act[7:]
            run("DELETE FROM accounts WHERE ott=?", (k,))
            AUTH_TOKENS.pop(k, None)
            OPENERS.pop(k, None)
            show(acc_text(), acc_kb())
        elif act == "links":
            rows = run("SELECT * FROM tracked ORDER BY id DESC LIMIT 15")
            if not rows:
                return show("📋 <b>Tracked Links</b>\n\nNothing yet. Tap 🔗 Add Link in the panel.", kb([[btn("« Panel", "a:panel")]]))
            lines = [f"{KIND_LABEL.get(r['kind'], '')} · {OTTS.get(r['ott'], r['ott'])} · {esc((r['title'] or '')[:40])}" for r in rows]
            text = "📋 <b>Tracked Links</b> (latest 15)\n\n" + "\n".join(f"{n + 1}. {l}" for n, l in enumerate(lines))
            keys = [[btn(f"📤 {n + 1}", f"a:push:{r['id']}", style="success"), btn(f"🗑 {n + 1}", f"a:del:{r['id']}", style="danger")] for n, r in enumerate(rows)]
            show(text, kb(keys + [[btn("« Panel", "a:panel")]]))
        elif act.startswith("push:"):
            answer(cid, "Sending…")
            bg(push_now, chat, int(act[5:]))
        elif act.startswith("del:"):
            tid = int(act[4:])
            run("DELETE FROM seen WHERE cid LIKE ?", (f"{tid}|%",))
            run("DELETE FROM tracked WHERE id=?", (tid,))
            answer(cid, "Removed")
            send(chat, "🗑 Link removed.")
        elif act == "test":
            sample = Item("zee5", "test", "series", "Sample Series", "Season 1", "101", "Sample Series - Test Episode", "Hindi",
                          time.strftime("%d-%m-%Y"), time.strftime("%I:%M %p"), CONTACT_URL, SUPPORT_URL)
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
                {"command": "start", "description": "Check bot status"},
                {"command": "help", "description": "Command instructions"},
                {"command": "plans", "description": "View available subscription plans"},
                {"command": "otts", "description": "List supported OTT platforms"},
                {"command": "about", "description": "Bot information"},
                {"command": "settings", "description": "Customize language settings"},
                {"command": "myplan", "description": "Check your current plan"},
                {"command": "redeem", "description": "Activate plan with redeem code"},
                {"command": "getpremium", "description": "Activate Premium Plan"},
                {"command": "id", "description": "Retrieve your Telegram ID"},
            ],
        )
    except TgError as e:
        log.warning("setup warning: %s", e)
    print(f"✅ Bot @{me.get('username')} is running. Admins: {sorted(ADMIN_IDS) or 'NONE - set ADMIN_IDS in .env!'}")
    bg(restore_sessions)
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
