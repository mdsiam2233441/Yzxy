# OTT Notification Bot

1. Rename `.env.example` to `.env`, fill BOT_TOKEN and ADMIN_IDS.
2. Run: `python bot.py`   (Python 3.8+, no pip install needed)

## Admin (/admin)
- Add Link: send serial / web series / movie links (hotstar, sunnxt, zee5, sonyliv, dangalplay, mojapp). One per line.
  Optional words after the link: `movie`, `webseries`, `serial`, and a language, e.g. `https://... serial Hindi`
- Tracked Links: send the latest episode now (📤) or remove a link (🗑).
- Accounts: `/setlogin zee5 email_or_username password` or `/setkey zee5 API_KEY`, then tap 🔑 to verify.
- Grant / revoke plans, redeem codes, broadcast, stats, OTT on/off.

## Notes
- New episodes of tracked series are detected automatically (every POLL_SECONDS) and sent with image, season, episode, date, time and links.
- Movie links are announced with the 📤 button.
- Passwords/API keys are stored in `bot.db` - keep that file private.
