# ⚔️ Blood & Coin — Discord Bot

An AI-powered Discord bot that runs a living fantasy RP economy for your server.
Powered by `meta-llama/llama-3.3-70b-instruct` via OpenRouter.

---

## Features

| Command | What it does |
|---|---|
| `/join` | Create your character, pick a faction |
| `/profile` | View your Coin, Blood, rank |
| `/decree respond` | React to today's AI-generated event, earn 50 Coin |
| `/duel @user` | Challenge someone — AI narrates the fight, winner takes 50 Coin |
| `/give @user amount` | Transfer Coin to another player |
| `/legend` | Submit a moment — AI turns it into Hall of Legends lore |
| `/forcepost` | (Admin) Post today's decree manually |
| `/addcoin` | (Admin) Manually award Coin |

The leaderboard updates live in `#leaderboard` after every action.
A Daily Decree posts automatically every morning at 9 AM IST.

---

## Step 1 — Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it "Blood & Coin"
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - Server Members Intent
   - Message Content Intent
5. Click **Reset Token** → copy it (this is your `DISCORD_TOKEN`)
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Read Messages`, `Manage Messages`, `Embed Links`
7. Open the generated URL → add the bot to your server

---

## Step 2 — Create Discord Channels

Create these text channels in your server:
- `daily-decree`
- `leaderboard`
- `hall-of-legends`

(Names must match exactly, or set custom names in env vars)

---

## Step 3 — Deploy on Railway (free, 24/7)

1. Push this folder to a GitHub repo
2. Go to https://railway.app → **New Project → Deploy from GitHub**
3. Select your repo
4. Go to **Variables** tab and add:

```
DISCORD_TOKEN=your_discord_bot_token
OPENROUTER_KEY=your_openrouter_api_key
DECREE_HOUR=9
TIMEZONE=Asia/Kolkata
```

Optional overrides:
```
DECREE_CHANNEL=daily-decree
LEADERBOARD_CHANNEL=leaderboard
LORE_CHANNEL=hall-of-legends
```

5. Railway auto-deploys. Your bot is live 24/7.

---

## Running Locally (optional)

```bash
pip install -r requirements.txt

export DISCORD_TOKEN=xxx
export OPENROUTER_KEY=xxx

python bot.py
```

---

## Factions (default)

- **Shadow Hand** — spies, assassins, information brokers
- **Iron Crown** — soldiers, warlords, honour-bound warriors  
- **The Unmarked** — outcasts, rogues, those loyal to no one

---

## How the economy works

- Everyone starts with **100 Coin**
- Daily Decree response → **+50 Coin, +10 Blood**
- Winning a duel → **+50 Coin, +20 Blood**
- Losing a duel → **-50 Coin**
- Giving Coin transfers it directly (no fee)
- Leaderboard ranks by Coin

---

## Customising

- Change factions: edit `FACTIONS` list in `bot.py`
- Change decree time: set `DECREE_HOUR` env var (24h format)
- Change timezone: set `TIMEZONE` env var (e.g. `US/Eastern`)
- Adjust Coin rewards: search for `+= 50` in `bot.py`
