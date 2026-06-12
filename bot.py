import discord
from discord.ext import commands, tasks
from discord import app_commands
import json, os, aiohttp, random, asyncpg
from datetime import datetime, time, timedelta
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN               = os.environ["DISCORD_TOKEN"]
OPENROUTER_KEY      = os.environ["OPENROUTER_KEY"]
DATABASE_URL        = os.environ["DATABASE_URL"]
MODEL               = "meta-llama/llama-3.3-70b-instruct"
DECREE_CHANNEL      = os.environ.get("DECREE_CHANNEL",      "daily-decree")
LEADERBOARD_CHANNEL = os.environ.get("LEADERBOARD_CHANNEL", "leaderboard")
LORE_CHANNEL        = os.environ.get("LORE_CHANNEL",        "hall-of-legends")
DECREE_HOUR         = int(os.environ.get("DECREE_HOUR", "21"))
TIMEZONE            = os.environ.get("TIMEZONE", "Asia/Kolkata")
FACTIONS            = ["Shadow Hand", "Iron Crown", "The Unmarked"]

BEATS      = {"Attack": "Trick", "Trick": "Defend", "Defend": "Attack"}
MOVE_EMOJI = {"Attack": "⚔️", "Defend": "🛡️", "Trick": "🎭"}

# Blood→Coin conversion rates (diminishing returns)
BLOOD_CONVERT_TIERS = [
    (500,  0.40),   # up to 500 Blood: 0.4 Coin per Blood
    (1000, 0.25),   # 501–1000: 0.25
    (9999, 0.12),   # 1001+: 0.12
]

STREAK_BONUSES = {
    3:  25,
    7:  75,
    14: 200,
    30: 500,
}

pending_duels:  dict[str, dict] = {}
active_quests:  dict[str, dict] = {}
active_fm:      dict[str, dict] = {}   # guild_id → active faction mission

db_pool: asyncpg.Pool = None


# ── DB setup ──────────────────────────────────────────────────────────────────
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                uid               TEXT PRIMARY KEY,
                username          TEXT,
                coin              INTEGER DEFAULT 100,
                blood             INTEGER DEFAULT 0,
                faction           TEXT,
                character         TEXT,
                decree_responded  BOOLEAN DEFAULT FALSE,
                steal_cooldown    TEXT,
                gamble_cooldown   TEXT,
                daily_cooldown    TEXT,
                duel_wins         INTEGER DEFAULT 0,
                duel_losses       INTEGER DEFAULT 0,
                daily_streak      INTEGER DEFAULT 0,
                last_streak_date  TEXT,
                pact_uid          TEXT,
                pact_since        TEXT,
                titles            TEXT DEFAULT '[]',
                prestige          INTEGER DEFAULT 0
            )
        """)
        # Migrate existing tables to add new columns safely
        for col, definition in [
            ("daily_cooldown",  "TEXT"),
            ("daily_streak",    "INTEGER DEFAULT 0"),
            ("last_streak_date","TEXT"),
            ("pact_uid",        "TEXT"),
            ("pact_since",      "TEXT"),
            ("titles",          "TEXT DEFAULT '[]'"),
            ("prestige",        "INTEGER DEFAULT 0"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE players ADD COLUMN {col} {definition}")
            except Exception:
                pass

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quests (
                id            SERIAL PRIMARY KEY,
                owner_uid     TEXT,
                title         TEXT NOT NULL,
                stages        JSONB NOT NULL,
                current_stage INTEGER NOT NULL DEFAULT 0,
                reward        INTEGER NOT NULL,
                status        TEXT NOT NULL DEFAULT 'active',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at    TIMESTAMPTZ NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                id           SERIAL PRIMARY KEY,
                name         TEXT NOT NULL,
                difficulty   INTEGER NOT NULL,
                reward_coin  INTEGER NOT NULL,
                reward_blood INTEGER NOT NULL,
                metadata     JSONB,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at   TIMESTAMPTZ NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS faction_missions (
                id            SERIAL PRIMARY KEY,
                guild_id      TEXT NOT NULL,
                faction       TEXT NOT NULL,
                title         TEXT NOT NULL,
                description   TEXT NOT NULL,
                goal          INTEGER NOT NULL,
                progress      INTEGER NOT NULL DEFAULT 0,
                reward_coin   INTEGER NOT NULL,
                reward_blood  INTEGER NOT NULL,
                bonus_war_pts INTEGER NOT NULL DEFAULT 50,
                status        TEXT NOT NULL DEFAULT 'active',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at    TIMESTAMPTZ NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pacts (
                uid_a      TEXT NOT NULL,
                uid_b      TEXT NOT NULL,
                formed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_bonus TIMESTAMPTZ,
                PRIMARY KEY (uid_a, uid_b)
            )
        """)
        defaults = {
            "decree":             "null",
            "leaderboard_msg_id": "null",
            "lore_count":         "0",
            "bounties":           "{}",
            "faction_scores":     json.dumps({f: 0 for f in FACTIONS}),
            "faction_week_start": str(datetime.utcnow().date()),
        }
        for k, v in defaults.items():
            await conn.execute(
                "INSERT INTO meta (key, value) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                k, v
            )


# ── Data helpers ──────────────────────────────────────────────────────────────
async def get_meta(key: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM meta WHERE key=$1", key)
    return json.loads(row["value"]) if row else None

async def set_meta(key: str, value) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO meta (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            key, json.dumps(value)
        )

async def load_player(uid: str) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM players WHERE uid=$1", uid)
    return dict(row) if row else None

async def upsert_player(uid: str, data: dict) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO players
                (uid, username, coin, blood, faction, character,
                 decree_responded, steal_cooldown, gamble_cooldown,
                 daily_cooldown, duel_wins, duel_losses,
                 daily_streak, last_streak_date,
                 pact_uid, pact_since, titles, prestige)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT (uid) DO UPDATE SET
                username          = EXCLUDED.username,
                coin              = EXCLUDED.coin,
                blood             = EXCLUDED.blood,
                faction           = EXCLUDED.faction,
                character         = EXCLUDED.character,
                decree_responded  = EXCLUDED.decree_responded,
                steal_cooldown    = EXCLUDED.steal_cooldown,
                gamble_cooldown   = EXCLUDED.gamble_cooldown,
                daily_cooldown    = EXCLUDED.daily_cooldown,
                duel_wins         = EXCLUDED.duel_wins,
                duel_losses       = EXCLUDED.duel_losses,
                daily_streak      = EXCLUDED.daily_streak,
                last_streak_date  = EXCLUDED.last_streak_date,
                pact_uid          = EXCLUDED.pact_uid,
                pact_since        = EXCLUDED.pact_since,
                titles            = EXCLUDED.titles,
                prestige          = EXCLUDED.prestige
        """,
            uid,
            data.get("username"),
            data.get("coin", 100),
            data.get("blood", 0),
            data.get("faction"),
            data.get("character"),
            data.get("decree_responded", False),
            data.get("steal_cooldown"),
            data.get("gamble_cooldown"),
            data.get("daily_cooldown"),
            data.get("duel_wins", 0),
            data.get("duel_losses", 0),
            data.get("daily_streak", 0),
            data.get("last_streak_date"),
            data.get("pact_uid"),
            data.get("pact_since"),
            json.dumps(data.get("titles", [])) if isinstance(data.get("titles"), list) else (data.get("titles") or "[]"),
            data.get("prestige", 0),
        )

async def load_all_players() -> dict[str, dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM players")
    return {row["uid"]: dict(row) for row in rows}

async def get_or_create_player(uid: str, username: str) -> dict:
    p = await load_player(uid)
    if p is None:
        p = {
            "uid": uid, "username": username, "coin": 100, "blood": 0,
            "faction": None, "character": None, "decree_responded": False,
            "steal_cooldown": None, "gamble_cooldown": None, "daily_cooldown": None,
            "duel_wins": 0, "duel_losses": 0, "daily_streak": 0,
            "last_streak_date": None, "pact_uid": None, "pact_since": None,
            "titles": [], "prestige": 0,
        }
        await upsert_player(uid, p)
    else:
        # Normalise titles from JSON string to list
        if isinstance(p.get("titles"), str):
            try:
                p["titles"] = json.loads(p["titles"])
            except Exception:
                p["titles"] = []
        if p.get("username") != username:
            p["username"] = username
            await upsert_player(uid, p)
    return p


# ── Quest DB helpers ──────────────────────────────────────────────────────────
async def create_quest_db(owner_uid, title, stages, reward, expires_at=None):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO quests (owner_uid, title, stages, reward, expires_at) VALUES ($1,$2,$3,$4,$5) RETURNING id",
            owner_uid, title, json.dumps(stages), reward, expires_at
        )
    return row["id"]

async def fetch_active_quest_by_owner(owner_uid):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM quests WHERE owner_uid=$1 AND status='active'", owner_uid
        )
    return dict(row) if row else None

async def update_quest_stage_db(quest_id, new_stage):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE quests SET current_stage=$1 WHERE id=$2", new_stage, quest_id)

async def complete_quest_db(quest_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE quests SET status='completed' WHERE id=$1", quest_id)

async def load_active_quests_from_db():
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM quests WHERE status='active' AND owner_uid IS NOT NULL"
            )
        for r in rows:
            stages = r["stages"]
            if isinstance(stages, str):
                stages = json.loads(stages)
            active_quests[r["owner_uid"]] = {
                "id":     r["id"],
                "title":  r["title"],
                "stages": stages,
                "stage":  r["current_stage"],
                "reward": r["reward"],
            }
    except Exception:
        pass


# ── Contract DB helpers ───────────────────────────────────────────────────────
async def create_contract_db(name, difficulty, reward_coin, reward_blood,
                              metadata=None, expires_at=None):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO contracts (name, difficulty, reward_coin, reward_blood, metadata, expires_at) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            name, difficulty, reward_coin, reward_blood,
            json.dumps(metadata or {}), expires_at
        )
    return row["id"]

async def fetch_contract_by_id(contract_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM contracts WHERE id=$1", contract_id)
    return dict(row) if row else None

async def fetch_active_contracts(limit=10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM contracts WHERE expires_at IS NULL OR expires_at > now() "
            "ORDER BY created_at DESC LIMIT $1", limit
        )
    return [dict(r) for r in rows]

async def expire_contract_db(contract_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE contracts SET expires_at = now() WHERE id=$1", contract_id)

async def delete_contract_db(contract_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM contracts WHERE id=$1", contract_id)

async def fetch_all_contracts(limit=50):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM contracts ORDER BY created_at DESC LIMIT $1", limit
        )
    return [dict(r) for r in rows]


# ── Faction Mission DB helpers ────────────────────────────────────────────────
async def create_faction_mission_db(guild_id, faction, title, description, goal,
                                     reward_coin, reward_blood, bonus_war_pts, expires_at=None):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO faction_missions "
            "(guild_id, faction, title, description, goal, reward_coin, reward_blood, bonus_war_pts, expires_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
            guild_id, faction, title, description, goal,
            reward_coin, reward_blood, bonus_war_pts, expires_at
        )
    return row["id"]

async def fetch_active_faction_mission(guild_id, faction):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM faction_missions WHERE guild_id=$1 AND faction=$2 AND status='active' "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC LIMIT 1",
            guild_id, faction
        )
    return dict(row) if row else None

async def increment_faction_mission_progress(mission_id, amount=1):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE faction_missions SET progress = progress + $1 WHERE id=$2",
            amount, mission_id
        )
        row = await conn.fetchrow("SELECT progress, goal FROM faction_missions WHERE id=$1", mission_id)
    return row["progress"], row["goal"]

async def complete_faction_mission_db(mission_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE faction_missions SET status='completed' WHERE id=$1", mission_id)


# ── Pact DB helpers ───────────────────────────────────────────────────────────
async def create_pact_db(uid_a, uid_b):
    a, b = sorted([uid_a, uid_b])
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pacts (uid_a, uid_b) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            a, b
        )

async def get_pact(uid_a, uid_b):
    a, b = sorted([uid_a, uid_b])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM pacts WHERE uid_a=$1 AND uid_b=$2", a, b)
    return dict(row) if row else None

async def update_pact_bonus_time(uid_a, uid_b):
    a, b = sorted([uid_a, uid_b])
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE pacts SET last_bonus=now() WHERE uid_a=$1 AND uid_b=$2", a, b
        )

async def delete_pact_db(uid_a, uid_b):
    a, b = sorted([uid_a, uid_b])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM pacts WHERE uid_a=$1 AND uid_b=$2", a, b)


# ── Shared helpers ─────────────────────────────────────────────────────────────
def is_registered(p: dict) -> bool:
    return bool(p.get("character") and p.get("faction"))

def cname(p: dict, fallback: str = "Unknown") -> str:
    return p.get("character") or fallback

def on_cooldown(ts: str | None, minutes: int) -> bool:
    if not ts:
        return False
    return datetime.utcnow() < datetime.fromisoformat(ts) + timedelta(minutes=minutes)

def cooldown_left(ts: str, minutes: int) -> str:
    rem = (datetime.fromisoformat(ts) + timedelta(minutes=minutes)) - datetime.utcnow()
    m, s = divmod(int(rem.total_seconds()), 60)
    return f"{m}m {s}s"

async def add_faction_score(faction, pts):
    if not faction:
        return
    scores = await get_meta("faction_scores") or {f: 0 for f in FACTIONS}
    scores[faction] = scores.get(faction, 0) + pts
    await set_meta("faction_scores", scores)

def get_titles(p: dict) -> list:
    t = p.get("titles", [])
    if isinstance(t, str):
        try:
            return json.loads(t)
        except Exception:
            return []
    return t or []

def award_title(p: dict, title: str) -> bool:
    """Returns True if the title is newly awarded."""
    titles = get_titles(p)
    if title not in titles:
        titles.append(title)
        p["titles"] = titles
        return True
    return False

def check_titles(p: dict) -> list[str]:
    """Check all milestone titles and award any newly earned ones."""
    new = []
    w = p.get("duel_wins", 0)
    b = p.get("blood", 0)
    s = p.get("daily_streak", 0)
    ps = p.get("prestige", 0)
    milestones = [
        (w >= 1,   "First Blood"),
        (w >= 5,   "Duelist"),
        (w >= 25,  "Veteran Blade"),
        (w >= 50,  "Champion"),
        (w >= 100, "Warlord"),
        (b >= 100, "Blooded"),
        (b >= 500, "Crimson"),
        (b >= 1000,"Bloodsoaked"),
        (s >= 7,   "Devoted"),
        (s >= 30,  "Eternal"),
        (ps >= 1,  "Reborn"),
        (ps >= 3,  "Ascendant"),
    ]
    for condition, title in milestones:
        if condition and award_title(p, title):
            new.append(title)
    return new

def compute_blood_to_coin(blood_amount: int) -> int:
    coin = 0
    remaining = blood_amount
    for cap, rate in BLOOD_CONVERT_TIERS:
        if remaining <= 0:
            break
        tier_blood = min(remaining, cap)
        coin += int(tier_blood * rate)
        remaining -= tier_blood
    return coin

async def try_advance_faction_mission(guild, player: dict, channel=None):
    """Increment the player's faction mission progress if one exists."""
    if not player.get("faction"):
        return
    gid = str(guild.id)
    fm = await fetch_active_faction_mission(gid, player["faction"])
    if not fm:
        return
    progress, goal = await increment_faction_mission_progress(fm["id"])
    if progress >= goal:
        await complete_faction_mission_db(fm["id"])
        await add_faction_score(player["faction"], fm["bonus_war_pts"])
        players = await load_all_players()
        rewarded = []
        for uid, p in players.items():
            if is_registered(p) and p.get("faction") == player["faction"]:
                p["coin"]  += fm["reward_coin"]
                p["blood"] += fm["reward_blood"]
                await upsert_player(uid, p)
                rewarded.append(cname(p))
        if channel:
            names = ", ".join(f"**{n}**" for n in rewarded)
            await channel.send(
                f"## 🏴 FACTION MISSION COMPLETE!\n"
                f"**{player['faction']}** has completed **{fm['title']}**!\n\n"
                f"Each member receives **+{fm['reward_coin']} Coin** and **+{fm['reward_blood']} Blood**.\n"
                f"Faction War bonus: **+{fm['bonus_war_pts']} pts**\n\n"
                f"Warriors: {names}"
            )

async def notify_bounty_target(guild, target_uid, amount, placer_name):
    """DM the target when a bounty is placed on them."""
    try:
        member = guild.get_member(int(target_uid))
        if member:
            await member.send(
                f"🎯 **Bounty Notice**\n\n"
                f"**{placer_name}** has placed a **{amount} Coin** bounty on your head in **{guild.name}**.\n"
                f"Watch your back. Anyone who defeats you in a duel collects automatically."
            )
    except Exception:
        pass


# ── AI helper ──────────────────────────────────────────────────────────────────
class AIError(Exception):
    pass

async def ask_ai(system: str, user: str, max_tokens: int = 400) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bloodcoin-bot",
    }
    payload = {
        "model": MODEL, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user",   "content": user}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    raise AIError(f"API returned {resp.status}")
                result = await resp.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    raise AIError("Empty response from AI")
                return content.strip()
    except AIError:
        raise
    except Exception as e:
        raise AIError(f"AI request failed: {e}") from e


# ── Bot + tree setup ──────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ── Leaderboard ───────────────────────────────────────────────────────────────
async def build_leaderboard() -> str:
    players = await load_all_players()
    registered = [(uid, p) for uid, p in players.items() if is_registered(p)]
    if not registered:
        return "No players yet. Use `/join` to enter the world."
    registered.sort(key=lambda x: x[1]["coin"], reverse=True)
    bounties       = await get_meta("bounties") or {}
    faction_scores = await get_meta("faction_scores") or {f: 0 for f in FACTIONS}
    medals = ["👑", "⚔️", "🗡️"]
    lines  = ["## 🏆  Blood & Coin — Rankings\n"]
    for i, (uid, p) in enumerate(registered):
        medal      = medals[i] if i < 3 else f"`#{i+1}`"
        bounty_tag = " 🎯" if uid in bounties else ""
        pact_tag   = " 🤝" if p.get("pact_uid") else ""
        w = p.get("duel_wins", 0)
        l = p.get("duel_losses", 0)
        s = p.get("daily_streak", 0)
        streak_tag = f" 🔥{s}" if s >= 3 else ""
        prestige   = p.get("prestige", 0)
        prestige_tag = f" ✨×{prestige}" if prestige else ""
        titles     = get_titles(p)
        title_tag  = f" *[{titles[-1]}]*" if titles else ""
        lines.append(
            f"{medal} **{p['character']}**{prestige_tag}{title_tag} ({p['faction']}){bounty_tag}{pact_tag}{streak_tag}\n"
            f"  💰 {p['coin']} Coin  •  🩸 {p['blood']} Blood  •  W/L {w}/{l}\n"
        )
    lines.append("\n**⚔️ Faction War (this week)**")
    for f, s in sorted(faction_scores.items(), key=lambda x: -x[1]):
        bar = "█" * max(1, s // 10)
        lines.append(f"  {f}: {s} pts  {bar}")
    lines.append(
        "\n*🎯 = bounty  •  🤝 = blood pact  •  🔥 = login streak  •  ✨ = prestige  •  /decree respond earns Coin*"
    )
    return "\n".join(lines)

async def refresh_leaderboard(guild: discord.Guild) -> None:
    ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL)
    if not ch:
        return
    content = await build_leaderboard()
    mid = await get_meta("leaderboard_msg_id")
    try:
        if mid:
            msg = await ch.fetch_message(int(mid))
            await msg.edit(content=content)
            return
    except Exception:
        pass
    msg = await ch.send(content)
    await set_meta("leaderboard_msg_id", str(msg.id))


# ── Duel system ───────────────────────────────────────────────────────────────
def resolve_moves(c_move, d_move, c_blood, d_blood):
    if BEATS[c_move] == d_move:
        mp_c, mp_d = 60, 0
    elif BEATS[d_move] == c_move:
        mp_c, mp_d = 0, 60
    else:
        mp_c = mp_d = 30
    total = (c_blood + d_blood) or 1
    bp_c  = int((c_blood / total) * 40)
    bp_d  = 40 - bp_c
    sc, sd = mp_c + bp_c, mp_d + bp_d
    if sc != sd:
        return ("challenger" if sc > sd else "defender", sc, sd)
    return (random.choice(["challenger", "defender"]), sc, sd)


class DefenderMoveView(discord.ui.View):
    def __init__(self, duel_key, defender_id):
        super().__init__(timeout=60)
        self.duel_key    = duel_key
        self.defender_id = defender_id
        self.resolved    = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.defender_id:
            await interaction.response.send_message("This isn't your duel.", ephemeral=True)
            return False
        return True

    async def resolve(self, interaction, d_move):
        if self.resolved:
            await interaction.response.send_message("Already resolved.", ephemeral=True)
            return
        self.resolved = True
        self.stop()
        duel = pending_duels.pop(self.duel_key, None)
        if not duel:
            await interaction.response.send_message("This duel expired.", ephemeral=True)
            return
        await interaction.response.defer()
        c_uid  = duel["challenger_id"]
        d_uid  = str(interaction.user.id)
        c_move = duel["challenger_move"]
        stake  = duel["coin_stake"]

        challenger = await get_or_create_player(c_uid, duel["challenger_name"])
        defender   = await get_or_create_player(d_uid, interaction.user.display_name)

        winner, sc, sd = resolve_moves(c_move, d_move, challenger["blood"], defender["blood"])
        c_name = cname(challenger, duel["challenger_name"])
        d_name = cname(defender, interaction.user.display_name)

        if winner == "challenger":
            challenger["coin"]      += stake * 2
            challenger["blood"]     += 20
            challenger["duel_wins"] += 1
            defender["duel_losses"] += 1
            await add_faction_score(challenger.get("faction"), 15)
            winner_mention = f"<@{c_uid}>"
            winner_name    = c_name
            winner_p       = challenger
            winner_uid     = c_uid
            loser_uid      = d_uid
        else:
            defender["coin"]          += stake * 2
            defender["blood"]         += 20
            defender["duel_wins"]     += 1
            challenger["duel_losses"] += 1
            await add_faction_score(defender.get("faction"), 15)
            winner_mention = interaction.user.mention
            winner_name    = d_name
            winner_p       = defender
            winner_uid     = d_uid
            loser_uid      = c_uid

        # Check titles
        new_titles_c = check_titles(challenger)
        new_titles_d = check_titles(defender)

        await upsert_player(c_uid, challenger)
        await upsert_player(d_uid, defender)

        bounty_msg = ""
        bounties   = await get_meta("bounties") or {}
        bounty     = bounties.get(loser_uid)
        if bounty and bounty.get("amount", 0) > 0:
            reward       = bounty["amount"]
            wp           = await load_player(winner_uid)
            wp["coin"]  += reward
            await upsert_player(winner_uid, wp)
            del bounties[loser_uid]
            await set_meta("bounties", bounties)
            bounty_msg = f"\n🎯 **BOUNTY CLAIMED!** {cname(wp)} collects **{reward} Coin** for the kill!"

        # Pact bonus — pact partner of winner gets 10 Blood
        pact_msg = ""
        if winner_p.get("pact_uid"):
            pact_partner = await load_player(winner_p["pact_uid"])
            if pact_partner:
                pact_partner["blood"] += 10
                await upsert_player(winner_p["pact_uid"], pact_partner)
                pact_msg = f"\n🤝 **{cname(pact_partner)}** (pact partner) gains **+10 Blood** from the victory!"

        title_msgs = []
        for t in new_titles_c:
            title_msgs.append(f"🏅 **{c_name}** earned the title **[{t}]**!")
        for t in new_titles_d:
            title_msgs.append(f"🏅 **{d_name}** earned the title **[{t}]**!")

        try:
            narrative = await ask_ai(
                "You are a battle chronicler for a gritty fantasy Discord RP. "
                "Write 3 dramatic sentences describing a duel based on the moves used. "
                "Make it feel cinematic. No emojis. No 'Winner:' line.",
                f"{c_name} (Blood {challenger['blood']}) used {c_move}.\n"
                f"{d_name} (Blood {defender['blood']}) used {d_move}.\n"
                f"{winner_name} wins. Narrate the fight."
            )
        except AIError:
            narrative = f"The clash was fierce and decisive. {winner_name} emerged victorious."

        out = (
            f"## ⚔️ DUEL RESOLVED\n<@{c_uid}> vs {interaction.user.mention}\n\n"
            f"{MOVE_EMOJI[c_move]} **{c_name}** → **{c_move}**  (`{sc}pts`)\n"
            f"{MOVE_EMOJI[d_move]} **{d_name}** → **{d_move}**  (`{sd}pts`)\n\n"
            f"*{narrative}*\n\n"
            f"🏆 {winner_mention} wins **{stake} Coin**!{bounty_msg}{pact_msg}"
        )
        if title_msgs:
            out += "\n\n" + "\n".join(title_msgs)
        await interaction.channel.send(out)

        # Try advance faction mission
        await try_advance_faction_mission(interaction.guild, winner_p, interaction.channel)
        await refresh_leaderboard(interaction.guild)

    @discord.ui.button(label="⚔️ Attack", style=discord.ButtonStyle.danger)
    async def attack(self, i, _): await self.resolve(i, "Attack")

    @discord.ui.button(label="🛡️ Defend", style=discord.ButtonStyle.primary)
    async def defend(self, i, _): await self.resolve(i, "Defend")

    @discord.ui.button(label="🎭 Trick", style=discord.ButtonStyle.secondary)
    async def trick(self, i, _): await self.resolve(i, "Trick")

    async def on_timeout(self):
        duel = pending_duels.pop(self.duel_key, None)
        if duel:
            cp = await load_player(duel["challenger_id"])
            if cp:
                cp["coin"] += duel["coin_stake"]
                await upsert_player(duel["challenger_id"], cp)


class ChallengerMoveView(discord.ui.View):
    def __init__(self, challenger_id, opponent, stake):
        super().__init__(timeout=60)
        self.challenger_id = challenger_id
        self.opponent      = opponent
        self.stake         = stake

    async def interaction_check(self, interaction):
        if interaction.user.id != self.challenger_id:
            await interaction.response.send_message("Not your duel.", ephemeral=True)
            return False
        return True

    async def pick(self, interaction, move):
        self.stop()
        challenger = await get_or_create_player(str(interaction.user.id), interaction.user.display_name)
        challenger["coin"] -= self.stake
        await upsert_player(str(interaction.user.id), challenger)
        duel_key = f"{interaction.user.id}-{self.opponent.id}-{int(datetime.utcnow().timestamp())}"
        pending_duels[duel_key] = {
            "challenger_id":   str(interaction.user.id),
            "challenger_name": interaction.user.display_name,
            "challenger_move": move,
            "coin_stake":      self.stake,
        }
        defender = await get_or_create_player(str(self.opponent.id), self.opponent.display_name)
        c_name   = cname(challenger, interaction.user.display_name)
        d_name   = cname(defender,   self.opponent.display_name)
        view = DefenderMoveView(duel_key, self.opponent.id)
        await interaction.response.send_message(
            f"## ⚔️ DUEL CHALLENGE\n"
            f"**{c_name}** challenges **{d_name}**!\n\n"
            f"Stake: **{self.stake} Coin**  •  🔒 Challenger's move is sealed\n\n"
            f"{self.opponent.mention} — pick your move! You have **60 seconds** or forfeit.",
            view=view
        )

    @discord.ui.button(label="⚔️ Attack", style=discord.ButtonStyle.danger)
    async def attack(self, i, _): await self.pick(i, "Attack")

    @discord.ui.button(label="🛡️ Defend", style=discord.ButtonStyle.primary)
    async def defend(self, i, _): await self.pick(i, "Defend")

    @discord.ui.button(label="🎭 Trick", style=discord.ButtonStyle.secondary)
    async def trick(self, i, _): await self.pick(i, "Trick")

    async def on_timeout(self):
        pass


# ── Pact UI ───────────────────────────────────────────────────────────────────
class PactConfirmView(discord.ui.View):
    def __init__(self, initiator: discord.Member, target: discord.Member,
                 initiator_uid: str, target_uid: str, blood_cost: int):
        super().__init__(timeout=120)
        self.initiator     = initiator
        self.target        = target
        self.initiator_uid = initiator_uid
        self.target_uid    = target_uid
        self.blood_cost    = blood_cost
        self.resolved      = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This pact request isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Accept the Pact", style=discord.ButtonStyle.success)
    async def accept(self, interaction, _):
        if self.resolved:
            return
        self.resolved = True
        self.stop()
        ip = await get_or_create_player(self.initiator_uid, self.initiator.display_name)
        tp = await get_or_create_player(self.target_uid,    self.target.display_name)
        # Break old pacts
        for p, uid in [(ip, self.initiator_uid), (tp, self.target_uid)]:
            if p.get("pact_uid"):
                old = await load_player(p["pact_uid"])
                if old:
                    old["pact_uid"]   = None
                    old["pact_since"] = None
                    await upsert_player(p["pact_uid"], old)
                await delete_pact_db(uid, p["pact_uid"])
        now_iso = datetime.utcnow().isoformat()
        ip["blood"]      -= self.blood_cost
        ip["pact_uid"]    = self.target_uid
        ip["pact_since"]  = now_iso
        tp["pact_uid"]    = self.initiator_uid
        tp["pact_since"]  = now_iso
        await upsert_player(self.initiator_uid, ip)
        await upsert_player(self.target_uid,    tp)
        await create_pact_db(self.initiator_uid, self.target_uid)
        await interaction.response.send_message(
            f"## 🤝 BLOOD PACT FORGED\n\n"
            f"**{cname(ip)}** and **{cname(tp)}** are now bound.\n\n"
            f"• Each duel victory grants the pact partner **+10 Blood**\n"
            f"• Use `/pactbonus` once per 12h to collect a shared Blood bonus\n"
            f"• Break the pact anytime with `/breakpact` (costs 50 Blood)"
        )
        await refresh_leaderboard(interaction.guild)

    @discord.ui.button(label="❌ Refuse", style=discord.ButtonStyle.danger)
    async def refuse(self, interaction, _):
        if self.resolved:
            return
        self.resolved = True
        self.stop()
        # Refund blood cost to initiator
        ip = await get_or_create_player(self.initiator_uid, self.initiator.display_name)
        ip["blood"] += self.blood_cost
        await upsert_player(self.initiator_uid, ip)
        await interaction.response.send_message(
            f"**{self.target.display_name}** refused the blood pact. "
            f"**{self.blood_cost} Blood** returned to {self.initiator.mention}."
        )

    async def on_timeout(self):
        ip = await load_player(self.initiator_uid)
        if ip:
            ip["blood"] += self.blood_cost
            await upsert_player(self.initiator_uid, ip)


# ── Quest generation helpers ───────────────────────────────────────────────────
async def generate_quest(character, faction):
    system = (
        "You are a quest designer for a gritty fantasy Discord RP set in Valdris. "
        "Create a 3-stage quest as JSON with this exact structure:\n"
        '{"title":"...", "intro":"...(2 sentences, hook the player)...", "stages":['
        '{"description":"...(what happens)...","prompt":"...(what should the player do?)..."},'
        '{"description":"...","prompt":"..."},'
        '{"description":"...","prompt":"..."}]}\n'
        "Make it dark, tense, and personal to the character. No emojis. JSON only, no markdown."
    )
    raw = await ask_ai(system,
        f"Character: {character}, Faction: {faction}. Design a quest.", max_tokens=600)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AIError(f"Quest JSON invalid: {e}") from e
    if not isinstance(data.get("title"), str):
        raise AIError("Quest missing title")
    if not isinstance(data.get("intro"), str):
        raise AIError("Quest missing intro")
    stages = data.get("stages", [])
    if len(stages) != 3 or not all(
        isinstance(s.get("description"), str) and isinstance(s.get("prompt"), str)
        for s in stages
    ):
        raise AIError("Quest stages malformed")
    return data

async def generate_quest_stage_outcome(quest_title, stage_desc, action, character, is_final):
    tone = "climactic and conclusive" if is_final else "tense and escalating"
    return await ask_ai(
        f"You are narrating a {tone} moment in a gritty fantasy quest called '{quest_title}'. "
        "Write 2-3 dramatic sentences describing the outcome of the player's action. No emojis.",
        f"Stage: {stage_desc}\n{character} does: {action}"
    )


# ── Slash commands ─────────────────────────────────────────────────────────────

@tree.command(name="join", description="Create your character and enter Valdris")
@app_commands.describe(character_name="Your character's name", faction="Your faction")
@app_commands.choices(faction=[app_commands.Choice(name=f, value=f) for f in FACTIONS])
async def join(interaction, character_name: str, faction: app_commands.Choice[str]):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if is_registered(player):
        await interaction.response.send_message(
            f"You're already **{player['character']}** of **{player['faction']}**. "
            "Use `/profile` to check your stats.", ephemeral=True
        )
        return
    player["character"] = character_name.strip()
    player["faction"]   = faction.value
    await upsert_player(uid, player)
    decree_ch = discord.utils.get(interaction.guild.text_channels, name=DECREE_CHANNEL)
    await interaction.response.send_message(
        f"⚔️ **{character_name}** has entered Valdris, sworn to **{faction.value}**.\n"
        f"Starting purse: 💰 100 Coin  •  🩸 0 Blood\n\n"
        f"**Get started:** `/daily` for your first bonus, then check the decree in "
        f"{'<#'+str(decree_ch.id)+'>' if decree_ch else '#'+DECREE_CHANNEL}."
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="profile", description="View your character stats")
async def profile(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    all_players = await load_all_players()
    ranked = sorted(
        [(u, p) for u, p in all_players.items() if is_registered(p)],
        key=lambda x: x[1]["coin"], reverse=True
    )
    rank     = next((i+1 for i, (u, _) in enumerate(ranked) if u == uid), "?")
    bounties = await get_meta("bounties") or {}
    bounty   = bounties.get(uid)
    titles   = get_titles(player)
    prestige = player.get("prestige", 0)
    streak   = player.get("daily_streak", 0)
    pact_uid = player.get("pact_uid")
    pact_name = None
    if pact_uid:
        pp = await load_player(pact_uid)
        if pp:
            pact_name = cname(pp)

    embed = discord.Embed(
        title=f"{'✨ ' * prestige}⚔️ {player['character']}{'  ✨' * prestige}",
        color=0x4B0082
    )
    embed.add_field(name="Faction",       value=player["faction"],    inline=True)
    embed.add_field(name="Rank",          value=f"#{rank}",           inline=True)
    embed.add_field(name="Prestige",      value=f"✨ ×{prestige}" if prestige else "—", inline=True)
    embed.add_field(name="💰 Coin",       value=str(player["coin"]),  inline=True)
    embed.add_field(name="🩸 Blood",      value=str(player["blood"]), inline=True)
    embed.add_field(name="🔥 Streak",     value=f"{streak} day{'s' if streak != 1 else ''}", inline=True)
    embed.add_field(name="Duel W/L",      value=f"{player.get('duel_wins',0)}/{player.get('duel_losses',0)}", inline=True)
    embed.add_field(name="🤝 Blood Pact", value=pact_name or "None",  inline=True)
    if titles:
        embed.add_field(name="🏅 Titles", value="  •  ".join(f"[{t}]" for t in titles), inline=False)
    if bounty:
        embed.set_footer(text=f"🎯 Bounty on your head: {bounty['amount']} Coin")
    await interaction.response.send_message(embed=embed)


@tree.command(name="daily", description="Claim your daily login reward (resets every 24h)")
async def daily(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if on_cooldown(player.get("daily_cooldown"), 1440):
        left = cooldown_left(player["daily_cooldown"], 1440)
        await interaction.response.send_message(
            f"Already claimed today. Come back in **{left}**.", ephemeral=True
        )
        return

    now       = datetime.utcnow()
    last_date = player.get("last_streak_date")
    streak    = player.get("daily_streak", 0)

    if last_date:
        last_dt = datetime.fromisoformat(last_date)
        days_since = (now.date() - last_dt.date()).days
        if days_since == 1:
            streak += 1
        elif days_since > 1:
            streak = 1
        else:
            streak = max(streak, 1)
    else:
        streak = 1

    base_coin  = 25
    base_blood = 5
    bonus_coin = 0
    streak_msg = ""

    # Streak milestones
    for threshold, bonus in sorted(STREAK_BONUSES.items()):
        if streak == threshold:
            bonus_coin  = bonus
            streak_msg  = f"\n🎉 **{streak}-day streak bonus: +{bonus} Coin!**"
            break

    # Pact passive: both players get +5 Blood if pact partner exists
    pact_msg = ""
    if player.get("pact_uid"):
        pact_partner = await load_player(player["pact_uid"])
        if pact_partner:
            pact_partner["blood"] += 5
            await upsert_player(player["pact_uid"], pact_partner)
            player["blood"] += 5
            base_blood += 5
            pact_msg = f"\n🤝 Blood Pact bonus: **+5 Blood** for you and **{cname(pact_partner)}**!"

    total_coin  = base_coin + bonus_coin
    player["coin"]            += total_coin
    player["blood"]           += base_blood
    player["daily_streak"]     = streak
    player["last_streak_date"] = now.isoformat()
    player["daily_cooldown"]   = now.isoformat()

    new_titles = check_titles(player)
    await upsert_player(uid, player)
    await add_faction_score(player.get("faction"), 2)

    streak_bar = "🔥" * min(streak, 10)
    title_msg  = ""
    if new_titles:
        title_msg = "\n" + "\n".join(f"🏅 New title unlocked: **[{t}]**!" for t in new_titles)

    # Next streak milestone hint
    next_milestone = next((t for t in sorted(STREAK_BONUSES) if t > streak), None)
    hint = f"\n*{next_milestone - streak} more day{'s' if next_milestone-streak != 1 else ''} until your next streak bonus!*" if next_milestone else ""

    await interaction.response.send_message(
        f"☀️ **Daily Reward Claimed!**\n\n"
        f"**{cname(player)}** — Day **{streak}** {streak_bar}\n\n"
        f"💰 +{total_coin} Coin  •  🩸 +{base_blood} Blood"
        f"{streak_msg}{pact_msg}{title_msg}{hint}"
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="spend", description="Convert Blood into Coin (diminishing returns)")
@app_commands.describe(amount="How much Blood to convert")
async def spend(interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if player["blood"] < amount:
        await interaction.response.send_message(
            f"Not enough Blood. You have **{player['blood']}**.", ephemeral=True
        )
        return
    coin_gain   = compute_blood_to_coin(amount)
    if coin_gain <= 0:
        await interaction.response.send_message(
            "That amount of Blood isn't worth converting.", ephemeral=True
        )
        return
    player["blood"] -= amount
    player["coin"]  += coin_gain
    await upsert_player(uid, player)

    # Rate display
    effective_rate = round(coin_gain / amount, 2)
    await interaction.response.send_message(
        f"🩸➡️💰 **Blood Cashed Out**\n\n"
        f"**{cname(player)}** converts **{amount} Blood** → **{coin_gain} Coin**\n"
        f"*(effective rate: {effective_rate} Coin/Blood — higher amounts yield less)*\n\n"
        f"New balance: 💰 {player['coin']}  •  🩸 {player['blood']}"
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="bloodpact", description="Forge a Blood Pact with another player (costs 50 Blood)")
@app_commands.describe(partner="Who to forge a pact with")
async def bloodpact(interaction, partner: discord.Member):
    if partner.id == interaction.user.id:
        await interaction.response.send_message("Can't pact with yourself.", ephemeral=True)
        return
    if partner.bot:
        await interaction.response.send_message("Can't pact with a bot.", ephemeral=True)
        return
    uid    = str(interaction.user.id)
    tuid   = str(partner.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    target = await get_or_create_player(tuid, partner.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if not is_registered(target):
        await interaction.response.send_message(f"**{partner.display_name}** hasn't joined yet.", ephemeral=True)
        return
    blood_cost = 50
    if player["blood"] < blood_cost:
        await interaction.response.send_message(
            f"You need **{blood_cost} Blood** to forge a pact. You have **{player['blood']}**.", ephemeral=True
        )
        return
    # Check existing pact
    if player.get("pact_uid") == tuid:
        await interaction.response.send_message(
            f"You're already pacted with **{cname(target)}**.", ephemeral=True
        )
        return
    # Deduct blood cost immediately; refunded on refuse/timeout
    player["blood"] -= blood_cost
    await upsert_player(uid, player)

    view = PactConfirmView(interaction.user, partner, uid, tuid, blood_cost)
    await interaction.response.send_message(
        f"## 🤝 Blood Pact Proposal\n\n"
        f"**{cname(player)}** offers a Blood Pact to **{cname(target)}**.\n\n"
        f"**Benefits:**\n"
        f"• Partner gains **+10 Blood** on every duel win you score\n"
        f"• `/pactbonus` every 12h for shared Blood income\n"
        f"• Daily login gives both partners an extra **+5 Blood**\n\n"
        f"Cost: **{blood_cost} Blood** (already deducted from {interaction.user.mention})\n\n"
        f"{partner.mention} — do you accept?",
        view=view
    )


@tree.command(name="pactbonus", description="Collect your 12-hour Blood Pact shared bonus")
async def pactbonus(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    pact_uid = player.get("pact_uid")
    if not pact_uid:
        await interaction.response.send_message(
            "You don't have an active Blood Pact. Use `/bloodpact @user` to forge one.", ephemeral=True
        )
        return
    pact = await get_pact(uid, pact_uid)
    if not pact:
        # Pact broken; clean up
        player["pact_uid"] = None
        await upsert_player(uid, player)
        await interaction.response.send_message("Your blood pact has been severed.", ephemeral=True)
        return
    last_bonus = pact.get("last_bonus")
    if last_bonus:
        lb_dt = last_bonus if isinstance(last_bonus, datetime) else datetime.fromisoformat(str(last_bonus))
        if lb_dt.tzinfo:
            lb_dt = lb_dt.replace(tzinfo=None)
        if datetime.utcnow() < lb_dt + timedelta(hours=12):
            rem = (lb_dt + timedelta(hours=12)) - datetime.utcnow()
            h, m = divmod(int(rem.total_seconds() // 60), 60)
            await interaction.response.send_message(
                f"Pact bonus not ready yet. Come back in **{h}h {m}m**.", ephemeral=True
            )
            return
    partner = await get_or_create_player(pact_uid, "")
    bonus   = 15  # Blood each
    player["blood"]  += bonus
    partner["blood"] += bonus
    await upsert_player(uid, player)
    await upsert_player(pact_uid, partner)
    await update_pact_bonus_time(uid, pact_uid)
    await interaction.response.send_message(
        f"🤝 **Pact Bonus Collected!**\n\n"
        f"**{cname(player)}** and **{cname(partner)}** each gain **+{bonus} Blood**.\n"
        f"Next bonus available in **12 hours**."
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="breakpact", description="Sever your Blood Pact (costs 50 Blood)")
async def breakpact(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    pact_uid = player.get("pact_uid")
    if not pact_uid:
        await interaction.response.send_message("You don't have an active pact.", ephemeral=True)
        return
    cost = 50
    if player["blood"] < cost:
        await interaction.response.send_message(
            f"Breaking a pact costs **{cost} Blood**. You have **{player['blood']}**.", ephemeral=True
        )
        return
    partner = await load_player(pact_uid)
    player["blood"]    -= cost
    player["pact_uid"]  = None
    player["pact_since"]= None
    await upsert_player(uid, player)
    if partner:
        partner["pact_uid"]  = None
        partner["pact_since"]= None
        await upsert_player(pact_uid, partner)
    await delete_pact_db(uid, pact_uid)
    partner_name = cname(partner) if partner else "your former partner"
    await interaction.response.send_message(
        f"🩸 **Blood Pact Severed**\n\n"
        f"**{cname(player)}** breaks the pact with **{partner_name}**. "
        f"**{cost} Blood** spilled in the severance."
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="prestige", description="Reset to 0 Coin & Blood, keep titles, gain Prestige rank (requires 1000 Coin + 500 Blood)")
async def prestige_cmd(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if player["coin"] < 1000 or player["blood"] < 500:
        await interaction.response.send_message(
            f"Prestige requires **1000 Coin** and **500 Blood**.\n"
            f"You have: 💰 {player['coin']}  •  🩸 {player['blood']}", ephemeral=True
        )
        return
    player["prestige"]     = player.get("prestige", 0) + 1
    player["coin"]          = 100
    player["blood"]         = 0
    player["daily_streak"]  = 0
    award_title(player, "Reborn")
    new_titles = check_titles(player)
    await upsert_player(uid, player)
    await add_faction_score(player.get("faction"), 50)
    title_msg = ""
    if new_titles:
        title_msg = "\n" + "\n".join(f"🏅 **[{t}]** unlocked!" for t in new_titles)
    await interaction.response.send_message(
        f"✨ **PRESTIGE ACHIEVED**\n\n"
        f"**{cname(player)}** transcends mortal limits.\n"
        f"Prestige rank: **✨ ×{player['prestige']}**\n\n"
        f"Stats reset. Titles and rank preserved.{title_msg}\n\n"
        f"*The cycle begins anew.*"
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="decree", description="Respond to today's Daily Decree to earn Coin")
@app_commands.describe(action="What does your character do?")
async def decree_respond(interaction, action: str):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if player.get("decree_responded"):
        await interaction.response.send_message("Already responded today.", ephemeral=True)
        return
    decree = await get_meta("decree")
    if not decree:
        await interaction.response.send_message("No decree posted yet today.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        outcome = await ask_ai(
            "You are a dramatic fantasy narrator. A player responded to today's event. "
            "Write ONE cinematic sentence (max 20 words) describing the outcome. No emojis.",
            f"Decree: {decree['text']}\n{player['character']} does: {action}"
        )
    except AIError:
        outcome = f"{player['character']} answered the call and made their mark on the city."
    player["coin"]             += 50
    player["blood"]            += 10
    player["decree_responded"]  = True
    await upsert_player(uid, player)
    await add_faction_score(player.get("faction"), 5)
    await try_advance_faction_mission(interaction.guild, player, interaction.channel)
    await interaction.followup.send(
        f"*{outcome}*\n\n**{player['character']}** earns **+50 Coin** and **+10 Blood**. 💰🩸"
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="duel", description="Challenge someone to a skill duel (50 Coin stake)")
@app_commands.describe(opponent="Who to challenge")
async def duel(interaction, opponent: discord.Member):
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("Can't duel yourself.", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("Can't duel a bot.", ephemeral=True)
        return
    uid        = str(interaction.user.id)
    challenger = await get_or_create_player(uid, interaction.user.display_name)
    defender   = await get_or_create_player(str(opponent.id), opponent.display_name)
    if not is_registered(challenger):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if not is_registered(defender):
        await interaction.response.send_message(f"**{opponent.display_name}** hasn't joined yet.", ephemeral=True)
        return
    if challenger["coin"] < 50:
        await interaction.response.send_message(
            f"Need at least **50 Coin** to duel. You have **{challenger['coin']}**.", ephemeral=True
        )
        return
    if defender["coin"] < 50:
        await interaction.response.send_message(
            f"**{cname(defender)}** is too broke to duel (needs 50 Coin).", ephemeral=True
        )
        return
    view = ChallengerMoveView(interaction.user.id, opponent, stake=50)
    await interaction.response.send_message(
        f"🔒 **{cname(challenger)}**, pick your move — only you can see this!",
        view=view, ephemeral=True
    )


@tree.command(name="steal", description="Attempt to steal Coin from another player (30 min cooldown)")
@app_commands.describe(target="Who to rob")
async def steal(interaction, target: discord.Member):
    if target.id == interaction.user.id:
        await interaction.response.send_message("Can't rob yourself.", ephemeral=True)
        return
    uid    = str(interaction.user.id)
    thief  = await get_or_create_player(uid, interaction.user.display_name)
    victim = await get_or_create_player(str(target.id), target.display_name)
    if not is_registered(thief):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if not is_registered(victim):
        await interaction.response.send_message(f"**{target.display_name}** hasn't joined yet.", ephemeral=True)
        return
    if on_cooldown(thief.get("steal_cooldown"), 30):
        left = cooldown_left(thief["steal_cooldown"], 30)
        await interaction.response.send_message(f"Lay low for **{left}** before stealing again.", ephemeral=True)
        return
    if victim["coin"] < 20:
        await interaction.response.send_message(f"**{cname(victim)}** has barely anything worth stealing.", ephemeral=True)
        return
    await interaction.response.defer()
    thief["steal_cooldown"] = datetime.utcnow().isoformat()
    blood_diff  = thief["blood"] - victim["blood"]
    success_pct = max(30, min(70, 50 + blood_diff // 5))
    success     = random.randint(1, 100) <= success_pct
    steal_amount = max(10, int(victim["coin"] * random.uniform(0.10, 0.30)))
    if success:
        steal_amount   = min(steal_amount, victim["coin"])
        thief["coin"]  += steal_amount
        victim["coin"] -= steal_amount
        await add_faction_score(thief.get("faction"), 5)
        outcome_prompt = (
            f"{cname(thief)} successfully stole {steal_amount} Coin from {cname(victim)}. "
            "Write 2 cinematic sentences about the heist. No emojis."
        )
    else:
        penalty = min(thief["coin"], 25)
        thief["coin"]   -= penalty
        victim["blood"] += 5
        outcome_prompt = (
            f"{cname(thief)} tried to rob {cname(victim)} but got caught. Lost {penalty} Coin. "
            "Write 2 cinematic sentences about getting caught. No emojis."
        )
    await upsert_player(uid, thief)
    await upsert_player(str(target.id), victim)
    try:
        narrative = await ask_ai(
            "You are a gritty fantasy narrator for a Discord RP. Be dramatic and brief.",
            outcome_prompt
        )
    except AIError:
        narrative = "The streets of Valdris never forget." if success else "The guards were watching."
    if success:
        await interaction.followup.send(
            f"🔪 **HEIST**\n\n*{narrative}*\n\n"
            f"**{cname(thief)}** pockets **{steal_amount} Coin** from **{cname(victim)}**. "
            f"(`{success_pct}%` success chance)"
        )
        await try_advance_faction_mission(interaction.guild, thief, interaction.channel)
    else:
        await interaction.followup.send(
            f"🔪 **CAUGHT!**\n\n*{narrative}*\n\n"
            f"**{cname(thief)}** failed and paid the price. "
            f"(`{success_pct}%` success chance)"
        )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="gamble", description="Bet Coin at The Rusty Crown tavern (10 min cooldown)")
@app_commands.describe(amount="How much to bet")
async def gamble(interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if on_cooldown(player.get("gamble_cooldown"), 10):
        left = cooldown_left(player["gamble_cooldown"], 10)
        await interaction.response.send_message(
            f"The tavern won't let you back at the table for **{left}**.", ephemeral=True
        )
        return
    if player["coin"] < amount:
        await interaction.response.send_message(f"Not enough Coin. You have **{player['coin']}**.", ephemeral=True)
        return
    await interaction.response.defer()
    player["gamble_cooldown"] = datetime.utcnow().isoformat()
    roll    = random.randint(1, 100)
    win_pct = 45
    won     = roll <= win_pct
    if won:
        multiplier = random.choice([1.5, 2.0, 2.5])
        winnings   = int(amount * multiplier) - amount
        player["coin"] += winnings
        result_line = f"**+{winnings} Coin** (×{multiplier})"
        colour      = 0x00FF00
    else:
        player["coin"] -= amount
        result_line = f"**-{amount} Coin**"
        colour      = 0xFF0000
    await upsert_player(uid, player)
    try:
        narrative = await ask_ai(
            "You are narrating a tense gambling scene in a gritty fantasy tavern called The Rusty Crown. "
            "Write ONE sentence about the dice roll outcome. Dramatic, no emojis.",
            f"Player: {cname(player)}. Bet: {amount} Coin. {'Won' if won else 'Lost'}."
        )
    except AIError:
        narrative = "The dice rolled. Fate decided."
    embed = discord.Embed(
        title=f"🎰 {'WINNER!' if won else 'BUST!'}",
        description=f"*{narrative}*",
        color=colour
    )
    embed.add_field(name="Bet",     value=f"{amount} Coin",        inline=True)
    embed.add_field(name="Result",  value=result_line,              inline=True)
    embed.add_field(name="Balance", value=f"{player['coin']} Coin", inline=True)
    embed.set_footer(text=f"Roll: {roll}/100  |  Win chance: {win_pct}%")
    await interaction.followup.send(embed=embed)
    await refresh_leaderboard(interaction.guild)


@tree.command(name="bounty", description="Place a bounty on someone's head")
@app_commands.describe(target="Who to mark", amount="How much Coin to offer")
async def bounty(interaction, target: discord.Member, amount: int):
    if target.id == interaction.user.id:
        await interaction.response.send_message("Can't bounty yourself.", ephemeral=True)
        return
    if amount < 50:
        await interaction.response.send_message("Minimum bounty is 50 Coin.", ephemeral=True)
        return
    uid    = str(interaction.user.id)
    placer = await get_or_create_player(uid, interaction.user.display_name)
    victim = await get_or_create_player(str(target.id), target.display_name)
    if not is_registered(placer):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if not is_registered(victim):
        await interaction.response.send_message(f"**{target.display_name}** hasn't joined yet.", ephemeral=True)
        return
    if placer["coin"] < amount:
        await interaction.response.send_message(f"Not enough Coin. You have **{placer['coin']}**.", ephemeral=True)
        return
    bounties  = await get_meta("bounties") or {}
    existing  = bounties.get(str(target.id), {})
    new_total = existing.get("amount", 0) + amount
    placer["coin"] -= amount
    await upsert_player(uid, placer)
    bounties[str(target.id)] = {
        "amount":         new_total,
        "placed_by_uid":  uid,
        "placed_by_name": cname(placer),
    }
    await set_meta("bounties", bounties)
    # DM the target
    await notify_bounty_target(interaction.guild, str(target.id), new_total, cname(placer))
    await interaction.response.send_message(
        f"🎯 **BOUNTY PLACED**\n\n"
        f"**{cname(placer)}** puts **{amount} Coin** on **{cname(victim)}'s** head.\n"
        f"Total bounty: **{new_total} Coin**\n\n"
        f"Anyone who defeats them in a duel collects automatically.\n"
        f"*(Target has been notified by DM.)*"
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="bounties", description="View all active bounties")
async def bounties_list(interaction):
    bounties = await get_meta("bounties") or {}
    active   = {uid: b for uid, b in bounties.items() if b.get("amount", 0) > 0}
    if not active:
        await interaction.response.send_message("No active bounties.", ephemeral=True)
        return
    lines   = ["## 🎯 Active Bounties\n"]
    players = await load_all_players()
    for uid, b in sorted(active.items(), key=lambda x: -x[1]["amount"]):
        p    = players.get(uid, {})
        name = cname(p, "Unknown")
        lines.append(f"**{name}** — **{b['amount']} Coin** (placed by {b['placed_by_name']})")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="quest", description="Begin a personal AI-generated quest")
async def quest(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if uid in active_quests:
        q = active_quests[uid]
        await interaction.response.send_message(
            f"Already on **{q['title']}** — Stage {q['stage']+1}/3.\n"
            "Use `/questcontinue <action>` to proceed.", ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        q_data = await generate_quest(cname(player), player["faction"])
    except Exception:
        await interaction.followup.send("The quest board is empty right now. Try again in a moment.", ephemeral=True)
        return
    reward_val = random.randint(150, 300)
    try:
        quest_id = await create_quest_db(uid, q_data["title"], q_data["stages"], reward_val)
    except Exception:
        quest_id = None
    active_quests[uid] = {
        "id":     quest_id,
        "title":  q_data["title"],
        "stages": q_data["stages"],
        "stage":  0,
        "reward": reward_val,
    }
    stage = q_data["stages"][0]
    embed = discord.Embed(
        title=f"🗺️ {q_data['title']}",
        description=q_data["intro"],
        color=0x2E8B57
    )
    embed.add_field(name="Stage 1/3", value=stage["description"], inline=False)
    embed.add_field(name="Your move", value=stage["prompt"],       inline=False)
    embed.add_field(name="💰 Reward", value=f"Up to {reward_val} Coin + 25 Blood", inline=False)
    embed.set_footer(text="Use /questcontinue <your action> to proceed")
    await interaction.followup.send(embed=embed)


@tree.command(name="questcontinue", description="Continue your active quest")
@app_commands.describe(action="What do you do?")
async def quest_continue(interaction, action: str):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if uid not in active_quests:
        try:
            row = await fetch_active_quest_by_owner(uid)
            if row:
                stages = row["stages"]
                if isinstance(stages, str):
                    stages = json.loads(stages)
                active_quests[uid] = {
                    "id":     row["id"],
                    "title":  row["title"],
                    "stages": stages,
                    "stage":  row["current_stage"],
                    "reward": row["reward"],
                }
        except Exception:
            pass
    if uid not in active_quests:
        await interaction.response.send_message("No active quest. Use `/quest` to begin one.", ephemeral=True)
        return
    await interaction.response.defer()
    q         = active_quests[uid]
    stage_idx = q["stage"]
    stage     = q["stages"][stage_idx]
    is_final  = (stage_idx == 2)
    try:
        outcome = await generate_quest_stage_outcome(
            q["title"], stage["description"], action, cname(player), is_final
        )
    except AIError:
        outcome = "The situation unfolded in unexpected ways."
    q["stage"] += 1
    try:
        if q.get("id"):
            await update_quest_stage_db(q["id"], q["stage"])
    except Exception:
        pass
    if is_final:
        reward = q["reward"]
        player["coin"]  += reward
        player["blood"] += 25
        new_titles = check_titles(player)
        await upsert_player(uid, player)
        await add_faction_score(player.get("faction"), 20)
        try:
            if q.get("id"):
                await complete_quest_db(q["id"])
        except Exception:
            pass
        del active_quests[uid]
        await try_advance_faction_mission(interaction.guild, player, interaction.channel)
        title_msg = ""
        if new_titles:
            title_msg = "\n" + "\n".join(f"🏅 **[{t}]** unlocked!" for t in new_titles)
        embed = discord.Embed(
            title=f"✅ Quest Complete — {q['title']}",
            description=outcome,
            color=0xFFD700
        )
        embed.set_footer(text=f"Reward: +{reward} Coin, +25 Blood")
        await interaction.followup.send(embed=embed)
        if title_msg:
            await interaction.followup.send(title_msg)
        await refresh_leaderboard(interaction.guild)
    else:
        next_stage = q["stages"][q["stage"]]
        embed = discord.Embed(
            title=f"🗺️ {q['title']} — Stage {q['stage']+1}/3",
            description=outcome,
            color=0x2E8B57
        )
        embed.add_field(name="What happens next", value=next_stage["description"], inline=False)
        embed.add_field(name="Your move",          value=next_stage["prompt"],      inline=False)
        embed.set_footer(text="Use /questcontinue <action> to proceed")
        await interaction.followup.send(embed=embed)


@tree.command(name="contracts", description="List available Shadow Contracts")
async def list_contracts(interaction):
    await interaction.response.defer()
    rows = await fetch_active_contracts()
    if not rows:
        await interaction.followup.send("No active contracts right now.")
        return
    lines = ["## 🪓 Shadow Contracts — Available"]
    for r in rows:
        lines.append(
            f"**ID {r['id']}** — {r['name']}  (Difficulty {r['difficulty']})\n"
            f"  Reward: {r['reward_coin']} Coin, {r['reward_blood']} Blood"
        )
    await interaction.followup.send("\n".join(lines))


@tree.command(name="acceptcontract", description="Accept and attempt a Shadow Contract")
@app_commands.describe(contract_id="ID of the contract to accept")
async def accept_contract(interaction, contract_id: int):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    contract = await fetch_contract_by_id(contract_id)
    if not contract or (
        contract.get("expires_at") and
        datetime.fromisoformat(str(contract["expires_at"])) <= datetime.utcnow()
    ):
        await interaction.response.send_message("Contract not found or expired.", ephemeral=True)
        return
    base         = 50
    diff_penalty = int(contract["difficulty"]) * 6
    blood_bonus  = min(30, player.get("blood", 0) // 5)
    success_chance = max(10, base - diff_penalty + blood_bonus)
    roll = random.randint(1, 100)
    await interaction.response.defer()
    if roll <= success_chance:
        player["coin"]  += int(contract["reward_coin"])
        player["blood"] += int(contract["reward_blood"])
        new_titles = check_titles(player)
        await upsert_player(uid, player)
        await add_faction_score(player.get("faction"), 10)
        await expire_contract_db(contract_id)
        await try_advance_faction_mission(interaction.guild, player, interaction.channel)
        title_msg = ""
        if new_titles:
            title_msg = "\n" + "\n".join(f"🏅 **[{t}]** unlocked!" for t in new_titles)
        await interaction.followup.send(
            f"✅ **Success!** You completed **{contract['name']}**.\n"
            f"Rewards: +{contract['reward_coin']} Coin, +{contract['reward_blood']} Blood{title_msg}"
        )
        await refresh_leaderboard(interaction.guild)
    else:
        loss = min(player.get("coin", 0), max(1, int(contract["reward_coin"]) // 3))
        player["coin"]  = max(0, player.get("coin", 0) - loss)
        player["blood"] = max(0, player.get("blood", 0) - max(0, int(contract["reward_blood"]) // 4))
        await upsert_player(uid, player)
        await expire_contract_db(contract_id)
        await interaction.followup.send(
            f"💀 You failed **{contract['name']}**. Lost {loss} Coin and some Blood."
        )


@tree.command(name="factionmission", description="View your faction's active mission")
async def factionmission(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    gid = str(interaction.guild.id)
    fm  = await fetch_active_faction_mission(gid, player["faction"])
    if not fm:
        await interaction.response.send_message(
            f"**{player['faction']}** has no active faction mission right now.\n"
            "Ask an admin to post one with `/postfactionmission`.", ephemeral=True
        )
        return
    progress = fm["progress"]
    goal     = fm["goal"]
    pct      = min(100, int(progress / goal * 100))
    bar_fill = int(pct / 10)
    bar      = "█" * bar_fill + "░" * (10 - bar_fill)
    embed = discord.Embed(
        title=f"🏴 Faction Mission — {fm['title']}",
        description=fm["description"],
        color=0x8B0000
    )
    embed.add_field(
        name=f"Progress  [{bar}] {pct}%",
        value=f"{progress} / {goal} actions completed",
        inline=False
    )
    embed.add_field(name="💰 Reward",    value=f"{fm['reward_coin']} Coin each",     inline=True)
    embed.add_field(name="🩸 Blood",     value=f"{fm['reward_blood']} Blood each",   inline=True)
    embed.add_field(name="⚔️ War Bonus", value=f"+{fm['bonus_war_pts']} faction pts", inline=True)
    embed.set_footer(text="Contribute by completing quests, contracts, decree responses, or steal successes.")
    await interaction.response.send_message(embed=embed)


@tree.command(name="factionwar", description="View current faction war standings")
async def faction_war(interaction):
    scores = await get_meta("faction_scores") or {f: 0 for f in FACTIONS}
    since  = await get_meta("faction_week_start") or "this week"
    lines  = [f"## ⚔️ Faction War Standings\n*Since {since}*\n"]
    for f, s in sorted(scores.items(), key=lambda x: -x[1]):
        bar = "█" * max(1, s // 10) if s else "░"
        lines.append(f"**{f}**: {s} pts  {bar}")
    lines.append("\n**How to earn faction points:**")
    lines.append("  Daily: +2  •  Decree: +5  •  Steal: +5  •  Contract: +10  •  Duel win: +15  •  Quest: +20  •  Prestige: +50")
    lines.append("\n*Winner announced every Monday — winning faction earns 200 Coin each.*")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="give", description="Transfer Coin to another player")
@app_commands.describe(member="Who to give to", amount="How much")
async def give(interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("Can't give to yourself.", ephemeral=True)
        return
    uid      = str(interaction.user.id)
    giver    = await get_or_create_player(uid, interaction.user.display_name)
    receiver = await get_or_create_player(str(member.id), member.display_name)
    if not is_registered(giver):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if giver["coin"] < amount:
        await interaction.response.send_message(f"Not enough Coin. You have **{giver['coin']}**.", ephemeral=True)
        return
    giver["coin"]    -= amount
    receiver["coin"] += amount
    await upsert_player(uid, giver)
    await upsert_player(str(member.id), receiver)
    await interaction.response.send_message(
        f"💰 **{cname(giver)}** sends **{amount} Coin** to **{cname(receiver, member.display_name)}**."
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="legend", description="Nominate a moment for the Hall of Legends")
@app_commands.describe(moment="Describe what happened")
async def legend(interaction, moment: str):
    await interaction.response.defer()
    try:
        lore = await ask_ai(
            "You are a chronicler for a Hall of Legends in a gritty fantasy Discord RP. "
            "Transform the moment into a dramatic third-person legend (3-4 sentences). "
            "Make it timeless and epic. No emojis.",
            f"Submitted by {interaction.user.display_name}: {moment}"
        )
    except AIError:
        lore = f"And so it was written: {moment}"
    lore_count = (await get_meta("lore_count") or 0) + 1
    await set_meta("lore_count", lore_count)
    embed = discord.Embed(title=f"📖 Legend #{lore_count}", description=lore, color=0xB8860B)
    embed.set_footer(text=f"Submitted by {interaction.user.display_name}")
    lore_ch = discord.utils.get(interaction.guild.text_channels, name=LORE_CHANNEL)
    if lore_ch:
        await lore_ch.send(embed=embed)
        await interaction.followup.send(f"Legend #{lore_count} inscribed.", ephemeral=True)
    else:
        await interaction.followup.send(embed=embed)


@tree.command(name="titles", description="View all unlockable titles and your progress")
async def titles_cmd(interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    earned = set(get_titles(player))
    w = player.get("duel_wins", 0)
    b = player.get("blood", 0)
    s = player.get("daily_streak", 0)
    ps = player.get("prestige", 0)
    all_titles = [
        ("First Blood",   f"Win 1 duel (you: {w}/1)",         w >= 1),
        ("Duelist",       f"Win 5 duels (you: {w}/5)",         w >= 5),
        ("Veteran Blade", f"Win 25 duels (you: {w}/25)",       w >= 25),
        ("Champion",      f"Win 50 duels (you: {w}/50)",       w >= 50),
        ("Warlord",       f"Win 100 duels (you: {w}/100)",     w >= 100),
        ("Blooded",       f"Reach 100 Blood (you: {b}/100)",   b >= 100),
        ("Crimson",       f"Reach 500 Blood (you: {b}/500)",   b >= 500),
        ("Bloodsoaked",   f"Reach 1000 Blood (you: {b}/1000)", b >= 1000),
        ("Devoted",       f"7-day streak (you: {s}/7)",        s >= 7),
        ("Eternal",       f"30-day streak (you: {s}/30)",      s >= 30),
        ("Reborn",        "Prestige once",                     ps >= 1),
        ("Ascendant",     "Prestige 3× (you: {ps}/3)",         ps >= 3),
    ]
    lines = ["## 🏅 Titles of Valdris\n"]
    for title, req, unlocked in all_titles:
        icon = "✅" if title in earned else ("🔓" if unlocked else "🔒")
        lines.append(f"{icon} **[{title}]** — {req}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ── Admin commands ─────────────────────────────────────────────────────────────

@tree.command(name="forcepost", description="[Admin] Post today's decree now")
@app_commands.checks.has_permissions(administrator=True)
async def forcepost(interaction):
    await interaction.response.defer(ephemeral=True)
    await post_decree(interaction.guild)
    await interaction.followup.send("Decree posted.", ephemeral=True)


@tree.command(name="forcefactionwar", description="[Admin] Resolve faction war now")
@app_commands.checks.has_permissions(administrator=True)
async def force_faction_war(interaction):
    await interaction.response.defer(ephemeral=True)
    await resolve_faction_war(interaction.guild)
    await interaction.followup.send("Faction war resolved.", ephemeral=True)


@tree.command(name="addcoin", description="[Admin] Add/remove Coin")
@app_commands.describe(member="Target", amount="Amount (negative to remove)")
@app_commands.checks.has_permissions(administrator=True)
async def addcoin(interaction, member: discord.Member, amount: int):
    uid    = str(member.id)
    player = await get_or_create_player(uid, member.display_name)
    player["coin"] = max(0, player["coin"] + amount)
    await upsert_player(uid, player)
    await interaction.response.send_message(
        f"Done. {member.display_name} now has {player['coin']} Coin.", ephemeral=True
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="addcontract", description="[Admin] Create a Shadow Contract")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(name="Contract name", difficulty="1-10", reward_coin="Coin reward",
                        reward_blood="Blood reward", expires_hours="Optional expiry in hours")
async def add_contract(interaction, name: str, difficulty: int,
                        reward_coin: int, reward_blood: int, expires_hours: int = None):
    if difficulty < 1 or difficulty > 10:
        await interaction.response.send_message("Difficulty must be 1–10.", ephemeral=True)
        return
    expires = None
    if expires_hours:
        expires = (datetime.utcnow() + timedelta(hours=expires_hours)).isoformat()
    cid = await create_contract_db(name, difficulty, reward_coin, reward_blood, {}, expires)
    await interaction.response.send_message(f"Contract **{name}** created (ID {cid}).", ephemeral=True)


@tree.command(name="deletecontract", description="[Admin] Delete a Shadow Contract")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(contract_id="ID of the contract to delete")
async def delete_contract(interaction, contract_id: int):
    contract = await fetch_contract_by_id(contract_id)
    if not contract:
        await interaction.response.send_message("Contract not found.", ephemeral=True)
        return

    await delete_contract_db(contract_id)
    await interaction.response.send_message(
        f"Contract **{contract['name']}** (ID {contract_id}) deleted.",
        ephemeral=True
    )


@tree.command(name="expirecontract", description="[Admin] Expire a Shadow Contract")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(contract_id="ID of the contract to expire")
async def expire_contract(interaction, contract_id: int):
    contract = await fetch_contract_by_id(contract_id)
    if not contract:
        await interaction.response.send_message("Contract not found.", ephemeral=True)
        return

    await expire_contract_db(contract_id)
    await interaction.response.send_message(
        f"Contract **{contract['name']}** (ID {contract_id}) expired.",
        ephemeral=True
    )


@tree.command(name="contractsadmin", description="[Admin] List all Shadow Contracts")
@app_commands.checks.has_permissions(administrator=True)
async def contracts_admin(interaction):
    rows = await fetch_all_contracts()
    if not rows:
        await interaction.response.send_message("No contracts found.", ephemeral=True)
        return

    lines = ["## 🪓 Shadow Contracts — Admin View"]
    for r in rows:
        expires = r['expires_at'] if r['expires_at'] else 'Never'
        status = 'Active' if not r['expires_at'] or datetime.fromisoformat(str(r['expires_at'])) > datetime.utcnow() else 'Expired'
        lines.append(
            f"**ID {r['id']}** — {r['name']} (Difficulty {r['difficulty']})\n"
            f"  Reward: {r['reward_coin']} Coin, {r['reward_blood']} Blood  •  {status}  •  Expires: {expires}"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="postfactionmission", description="[Admin] Post a faction mission")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    faction="Which faction",
    title="Mission title",
    description="What the faction must do",
    goal="Number of contributing actions required",
    reward_coin="Coin reward per member",
    reward_blood="Blood reward per member",
    bonus_war_pts="Faction war points on completion",
    expires_hours="Optional expiry in hours",
)
@app_commands.choices(faction=[app_commands.Choice(name=f, value=f) for f in FACTIONS])
async def post_faction_mission(
    interaction,
    faction: app_commands.Choice[str],
    title: str,
    description: str,
    goal: int,
    reward_coin: int,
    reward_blood: int,
    bonus_war_pts: int = 50,
    expires_hours: int = None,
):
    if goal < 1:
        await interaction.response.send_message("Goal must be at least 1.", ephemeral=True)
        return
    expires = None
    if expires_hours:
        expires = (datetime.utcnow() + timedelta(hours=expires_hours)).isoformat()
    gid = str(interaction.guild.id)
    mid = await create_faction_mission_db(
        gid, faction.value, title, description, goal,
        reward_coin, reward_blood, bonus_war_pts, expires
    )
    # Announce in leaderboard channel
    ch = discord.utils.get(interaction.guild.text_channels, name=LEADERBOARD_CHANNEL)
    embed = discord.Embed(
        title=f"🏴 NEW FACTION MISSION — {faction.value}",
        description=description,
        color=0x8B0000
    )
    embed.add_field(name="Mission",     value=title,             inline=False)
    embed.add_field(name="Goal",        value=f"{goal} actions", inline=True)
    embed.add_field(name="💰 Reward",   value=f"{reward_coin} Coin each",  inline=True)
    embed.add_field(name="🩸 Blood",    value=f"{reward_blood} Blood each", inline=True)
    embed.add_field(name="⚔️ War Pts",  value=f"+{bonus_war_pts}",          inline=True)
    embed.set_footer(text="Contribute via quests, contracts, decree responses, or steal successes.")
    if ch:
        await ch.send(embed=embed)
    await interaction.response.send_message(
        f"Faction mission **{title}** posted for **{faction.value}** (ID {mid}).", ephemeral=True
    )


@tree.command(name="resetplayer", description="[Admin] Wipe a player so they can /join again")
@app_commands.describe(member="Who to reset")
@app_commands.checks.has_permissions(administrator=True)
async def resetplayer(interaction, member: discord.Member):
    uid = str(member.id)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE uid=$1", uid)
    active_quests.pop(uid, None)
    await interaction.response.send_message(f"{member.display_name} reset.", ephemeral=True)


@tree.command(name="clearbounty", description="[Admin] Remove a bounty")
@app_commands.describe(member="Who to clear")
@app_commands.checks.has_permissions(administrator=True)
async def clearbounty(interaction, member: discord.Member):
    bounties = await get_meta("bounties") or {}
    bounties.pop(str(member.id), None)
    await set_meta("bounties", bounties)
    await interaction.response.send_message("Bounty cleared.", ephemeral=True)


# ── Scheduled tasks ───────────────────────────────────────────────────────────
async def generate_decree() -> str:
    players  = await load_all_players()
    factions = list({p["faction"] for p in players.values() if p.get("faction")}) or FACTIONS
    return await ask_ai(
        "You are the herald of Valdris, a gritty fantasy city. Write dramatic Daily Decrees. "
        "Each decree: 3-5 sentences, a dramatic situation + what members must DO + deadline flavour. "
        "Tone: dark, immersive, high stakes. No emojis. Under 120 words.",
        f"Active factions: {', '.join(factions)}. Write today's Daily Decree."
    )

async def post_decree(guild: discord.Guild) -> None:
    ch = discord.utils.get(guild.text_channels, name=DECREE_CHANNEL)
    if not ch:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET decree_responded=FALSE")
    try:
        text = await generate_decree()
    except AIError:
        text = "The city of Valdris stirs. Seek your fortune before the night ends."
    await set_meta("decree", {"text": text, "date": str(datetime.utcnow().date())})
    embed = discord.Embed(title="📜 The Daily Decree", description=text, color=0x8B1A1A)
    embed.set_footer(text="Use /decree respond <your action> before midnight to earn 50 Coin")
    await ch.send(embed=embed)

async def resolve_faction_war(guild: discord.Guild) -> None:
    scores = await get_meta("faction_scores") or {}
    if not any(scores.values()):
        return
    winner  = max(scores, key=lambda f: scores[f])
    bonus   = 200
    players = await load_all_players()
    winners = []
    for uid, p in players.items():
        if is_registered(p) and p["faction"] == winner:
            p["coin"] += bonus
            await upsert_player(uid, p)
            winners.append(cname(p))
    await set_meta("faction_scores", {f: 0 for f in FACTIONS})
    await set_meta("faction_week_start", str(datetime.utcnow().date()))
    ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL)
    if ch and winners:
        names = ", ".join(f"**{n}**" for n in winners)
        await ch.send(
            f"## ⚔️ FACTION WAR RESULTS\n"
            f"**{winner}** dominated this week — each member earns **{bonus} Coin**!\n"
            f"Champions: {names}\n\n*New week begins now. Fight for your faction.*"
        )
    await refresh_leaderboard(guild)

@tasks.loop(time=time(hour=DECREE_HOUR, tzinfo=pytz.timezone(TIMEZONE)))
async def daily_decree_task():
    for guild in bot.guilds:
        await post_decree(guild)

@tasks.loop(time=time(hour=9, tzinfo=pytz.timezone(TIMEZONE)))
async def weekly_faction_task():
    now = datetime.now(pytz.timezone(TIMEZONE))
    if now.weekday() == 0:
        for guild in bot.guilds:
            await resolve_faction_war(guild)


# ── Boot ───────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    await load_active_quests_from_db()
    await tree.sync()
    if not daily_decree_task.is_running():
        daily_decree_task.start()
    if not weekly_faction_task.is_running():
        weekly_faction_task.start()
    print(f"Blood & Coin v1.6.0 online as {bot.user}  |  Guilds: {len(bot.guilds)}")

bot.run(TOKEN)