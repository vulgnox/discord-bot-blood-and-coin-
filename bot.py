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

pending_duels: dict[str, dict] = {}
active_quests: dict[str, dict] = {}

db_pool: asyncpg.Pool = None


# ── DB setup ──────────────────────────────────────────────────────────────────
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                uid             TEXT PRIMARY KEY,
                username        TEXT,
                coin            INTEGER DEFAULT 100,
                blood           INTEGER DEFAULT 0,
                faction         TEXT,
                character       TEXT,
                decree_responded BOOLEAN DEFAULT FALSE,
                steal_cooldown  TEXT,
                gamble_cooldown TEXT,
                duel_wins       INTEGER DEFAULT 0,
                duel_losses     INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Seed meta defaults if not present
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
        # Ensure quests and contracts tables exist (runtime safety)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quests (
                id SERIAL PRIMARY KEY,
                owner_uid TEXT,
                title TEXT NOT NULL,
                stages JSONB NOT NULL,
                current_stage INTEGER NOT NULL DEFAULT 0,
                reward INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                difficulty INTEGER NOT NULL,
                reward_coin INTEGER NOT NULL,
                reward_blood INTEGER NOT NULL,
                metadata JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NULL
            )
        """)


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
                 duel_wins, duel_losses)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (uid) DO UPDATE SET
                username         = EXCLUDED.username,
                coin             = EXCLUDED.coin,
                blood            = EXCLUDED.blood,
                faction          = EXCLUDED.faction,
                character        = EXCLUDED.character,
                decree_responded = EXCLUDED.decree_responded,
                steal_cooldown   = EXCLUDED.steal_cooldown,
                gamble_cooldown  = EXCLUDED.gamble_cooldown,
                duel_wins        = EXCLUDED.duel_wins,
                duel_losses      = EXCLUDED.duel_losses
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
            data.get("duel_wins", 0),
            data.get("duel_losses", 0),
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
            "steal_cooldown": None, "gamble_cooldown": None,
            "duel_wins": 0, "duel_losses": 0,
        }
        await upsert_player(uid, p)
    else:
        if p.get("username") != username:
            p["username"] = username
            await upsert_player(uid, p)
    return p


# ── Quest & Contract DB helpers ───────────────────────────────────────────────
async def create_quest_db(owner_uid: str | None, title: str, stages: list, reward: int, expires_at: str | None = None) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO quests (owner_uid, title, stages, reward, expires_at) VALUES ($1,$2,$3,$4,$5) RETURNING id",
            owner_uid, title, json.dumps(stages), reward, expires_at
        )
    return row["id"]

async def fetch_active_quest_by_owner(owner_uid: str) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM quests WHERE owner_uid=$1 AND status='active'", owner_uid)
    return dict(row) if row else None

async def fetch_active_board_quests(limit: int = 10) -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM quests WHERE owner_uid IS NULL AND status='active' ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]

async def update_quest_stage_db(quest_id: int, new_stage: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE quests SET current_stage=$1 WHERE id=$2", new_stage, quest_id)

async def complete_quest_db(quest_id: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE quests SET status='completed' WHERE id=$1", quest_id)


async def create_contract_db(name: str, difficulty: int, reward_coin: int, reward_blood: int, metadata: dict | None = None, expires_at: str | None = None) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO contracts (name, difficulty, reward_coin, reward_blood, metadata, expires_at) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            name, difficulty, reward_coin, reward_blood, json.dumps(metadata or {}), expires_at
        )
    return row["id"]

async def fetch_contract_by_id(contract_id: int) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM contracts WHERE id=$1", contract_id)
    return dict(row) if row else None

async def load_active_quests_from_db() -> None:
    """Load active personal quests into in-memory cache on startup (optional)."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM quests WHERE status='active' AND owner_uid IS NOT NULL")
        for r in rows:
            active_quests[r["owner_uid"]] = {
                "id": r["id"],
                "title": r["title"],
                "stages": r["stages"],
                "stage": r["current_stage"],
                "reward": r["reward"],
            }
    except Exception:
        # tolerate errors at startup — DB may be unavailable during migrations
        pass


# ── Contracts helpers & commands ─────────────────────────────────────────────
async def fetch_active_contracts(limit: int = 10) -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM contracts WHERE expires_at IS NULL OR expires_at > now() ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]

async def expire_contract_db(contract_id: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE contracts SET expires_at = now() WHERE id=$1", contract_id)


@tree.command(name="contracts", description="List available Shadow Contracts")
async def list_contracts(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await fetch_active_contracts()
    if not rows:
        await interaction.followup.send("No active contracts right now.")
        return
    lines = ["## 🪓 Shadow Contracts — Available" ]
    for r in rows:
        lines.append(f"**ID {r['id']}** — {r['name']}  (Difficulty {r['difficulty']})\n  Reward: {r['reward_coin']} Coin, {r['reward_blood']} Blood")
    await interaction.followup.send("\n".join(lines))


@tree.command(name="addcontract", description="[Admin] Create a Shadow Contract")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(name="Contract name", difficulty="1-5", reward_coin="Coin reward", reward_blood="Blood reward", expires_hours="Optional expiry in hours")
async def add_contract(interaction: discord.Interaction, name: str, difficulty: int, reward_coin: int, reward_blood: int, expires_hours: int = None):
    if difficulty < 1 or difficulty > 10:
        await interaction.response.send_message("Difficulty must be between 1 and 10.", ephemeral=True)
        return
    expires = None
    if expires_hours:
        expires = (datetime.utcnow() + timedelta(hours=expires_hours)).isoformat()
    cid = await create_contract_db(name, difficulty, reward_coin, reward_blood, {}, expires)
    await interaction.response.send_message(f"Contract created (ID {cid}).", ephemeral=True)


@tree.command(name="acceptcontract", description="Accept and attempt a Shadow Contract")
@app_commands.describe(contract_id="ID of the contract to accept")
async def accept_contract(interaction: discord.Interaction, contract_id: int):
    uid = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)
    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    contract = await fetch_contract_by_id(contract_id)
    if not contract or (contract.get('expires_at') and datetime.fromisoformat(contract['expires_at']) <= datetime.utcnow()):
        await interaction.response.send_message("Contract not found or expired.", ephemeral=True)
        return

    # Compute success chance: base 50% modified by difficulty and player's blood
    base = 50
    diff_penalty = int(contract['difficulty']) * 6  # bigger difficulty reduces chance
    blood_bonus = min(30, player.get('blood', 0) // 5)
    success_chance = max(10, base - diff_penalty + blood_bonus)

    roll = random.randint(1, 100)
    await interaction.response.defer()
    if roll <= success_chance:
        # success
        player['coin'] += int(contract['reward_coin'])
        player['blood'] += int(contract['reward_blood'])
        await upsert_player(uid, player)
        await add_faction_score(player.get('faction'), 10)
        await expire_contract_db(contract_id)
        await interaction.followup.send(f"✅ Success! You completed **{contract['name']}**. Rewards: +{contract['reward_coin']} Coin, +{contract['reward_blood']} Blood")
        await refresh_leaderboard(interaction.guild)
    else:
        # failure penalty
        loss = min(player.get('coin', 0), max(1, int(contract['reward_coin']) // 3))
        player['coin'] = max(0, player.get('coin', 0) - loss)
        player['blood'] = max(0, player.get('blood', 0) - max(0, int(contract['reward_blood']) // 4))
        await upsert_player(uid, player)
        await expire_contract_db(contract_id)
        await interaction.followup.send(f"💀 You failed **{contract['name']}**. You lost {loss} Coin and some Blood.")


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

async def add_faction_score(faction: str | None, pts: int) -> None:
    if not faction:
        return
    scores = await get_meta("faction_scores") or {f: 0 for f in FACTIONS}
    scores[faction] = scores.get(faction, 0) + pts
    await set_meta("faction_scores", scores)


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


# ── Bot setup ──────────────────────────────────────────────────────────────────
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
        w = p.get("duel_wins", 0); l = p.get("duel_losses", 0)
        lines.append(
            f"{medal} **{p['character']}** ({p['faction']}){bounty_tag}\n"
            f"  💰 {p['coin']} Coin  •  🩸 {p['blood']} Blood  •  W/L {w}/{l}\n"
        )
    lines.append("\n**⚔️ Faction War (this week)**")
    for f, s in sorted(faction_scores.items(), key=lambda x: -x[1]):
        bar = "█" * max(1, s // 10)
        lines.append(f"  {f}: {s} pts  {bar}")
    lines.append("\n*🎯 = bounty on head  |  /decree respond to earn Coin*")
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


# ── Daily Decree ───────────────────────────────────────────────────────────────
async def generate_decree() -> str:
    players = await load_all_players()
    factions = list({p["faction"] for p in players.values() if p.get("faction")}) or FACTIONS
    return await ask_ai(
        "You are the herald of Valdris, a gritty fantasy city. Write dramatic Daily Decrees. "
        "Each decree: 3-5 sentences, a dramatic situation + what members must DO + deadline flavour. "
        "Tone: dark, immersive, high stakes. No emojis. Under 120 words.",
        f"Active factions: {', '.join(factions)}. Write today's Daily Decree."
    )

@tasks.loop(time=time(hour=DECREE_HOUR, tzinfo=pytz.timezone(TIMEZONE)))
async def daily_decree_task():
    for guild in bot.guilds:
        await post_decree(guild)

async def post_decree(guild: discord.Guild) -> None:
    ch = discord.utils.get(guild.text_channels, name=DECREE_CHANNEL)
    if not ch:
        return
    # Reset all players' decree_responded
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET decree_responded=FALSE")
    text = await generate_decree()
    await set_meta("decree", {"text": text, "date": str(datetime.utcnow().date())})
    embed = discord.Embed(title="📜 The Daily Decree", description=text, color=0x8B1A1A)
    embed.set_footer(text="Use /decree respond <your action> before midnight to earn 50 Coin")
    await ch.send(embed=embed)


# ── Weekly Faction War ─────────────────────────────────────────────────────────
@tasks.loop(time=time(hour=9, tzinfo=pytz.timezone(TIMEZONE)))
async def weekly_faction_task():
    now = datetime.now(pytz.timezone(TIMEZONE))
    if now.weekday() == 0:
        for guild in bot.guilds:
            await resolve_faction_war(guild)

async def resolve_faction_war(guild: discord.Guild) -> None:
    scores = await get_meta("faction_scores") or {}
    if not any(scores.values()):
        return

    winner = max(scores, key=lambda f: scores[f])
    bonus  = 200
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
            f"**{winner}** dominated this week and each member earns **{bonus} Coin**!\n"
            f"Champions: {names}\n\n*New week begins now. Fight for your faction.*"
        )
    await refresh_leaderboard(guild)


# ── Duel system ───────────────────────────────────────────────────────────────
def resolve_moves(c_move: str, d_move: str, c_blood: int, d_blood: int) -> tuple[str, int, int]:
    if BEATS[c_move] == d_move:
        mp_c, mp_d = 60, 0
    elif BEATS[d_move] == c_move:
        mp_c, mp_d = 0, 60
    else:
        mp_c = mp_d = 30
    total  = (c_blood + d_blood) or 1
    bp_c   = int((c_blood / total) * 40)
    bp_d   = 40 - bp_c
    sc, sd = mp_c + bp_c, mp_d + bp_d
    if sc != sd:
        return ("challenger" if sc > sd else "defender", sc, sd)
    return (random.choice(["challenger", "defender"]), sc, sd)


class DefenderMoveView(discord.ui.View):
    def __init__(self, duel_key: str, defender_id: int):
        super().__init__(timeout=60)
        self.duel_key    = duel_key
        self.defender_id = defender_id
        self.resolved    = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.defender_id:
            await interaction.response.send_message("This isn't your duel.", ephemeral=True)
            return False
        return True

    async def resolve(self, interaction: discord.Interaction, d_move: str) -> None:
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
        d_name = cname(defender,   interaction.user.display_name)

        if winner == "challenger":
            challenger["coin"]      += stake * 2
            challenger["blood"]     += 20
            challenger["duel_wins"] += 1
            defender["duel_losses"] += 1
            await add_faction_score(challenger.get("faction"), 15)
            winner_mention = f"<@{c_uid}>"
            winner_name    = c_name
        else:
            defender["coin"]          += stake * 2
            defender["blood"]         += 20
            defender["duel_wins"]     += 1
            challenger["duel_losses"] += 1
            await add_faction_score(defender.get("faction"), 15)
            winner_mention = interaction.user.mention
            winner_name    = d_name

        await upsert_player(c_uid, challenger)
        await upsert_player(d_uid, defender)

        bounty_msg  = ""
        loser_uid   = d_uid if winner == "challenger" else c_uid
        bounties    = await get_meta("bounties") or {}
        bounty      = bounties.get(loser_uid)
        if bounty and bounty.get("amount", 0) > 0:
            reward  = bounty["amount"]
            w_uid   = c_uid if winner == "challenger" else d_uid
            wp      = await load_player(w_uid)
            wp["coin"] += reward
            await upsert_player(w_uid, wp)
            del bounties[loser_uid]
            await set_meta("bounties", bounties)
            bounty_msg = f"\n🎯 **BOUNTY CLAIMED!** {cname(wp)} collects **{reward} Coin** for the kill!"

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

        await interaction.channel.send(
            f"## ⚔️ DUEL RESOLVED\n<@{c_uid}> vs {interaction.user.mention}\n\n"
            f"{MOVE_EMOJI[c_move]} **{c_name}** → **{c_move}**  (`{sc}pts`)\n"
            f"{MOVE_EMOJI[d_move]} **{d_name}** → **{d_move}**  (`{sd}pts`)\n\n"
            f"*{narrative}*\n\n"
            f"🏆 {winner_mention} wins **{stake} Coin**!{bounty_msg}"
        )
        await refresh_leaderboard(interaction.guild)

    @discord.ui.button(label="⚔️ Attack", style=discord.ButtonStyle.danger)
    async def attack(self, i: discord.Interaction, _: discord.ui.Button):
        await self.resolve(i, "Attack")

    @discord.ui.button(label="🛡️ Defend", style=discord.ButtonStyle.primary)
    async def defend(self, i: discord.Interaction, _: discord.ui.Button):
        await self.resolve(i, "Defend")

    @discord.ui.button(label="🎭 Trick", style=discord.ButtonStyle.secondary)
    async def trick(self, i: discord.Interaction, _: discord.ui.Button):
        await self.resolve(i, "Trick")

    async def on_timeout(self):
        duel = pending_duels.pop(self.duel_key, None)
        if duel:
            cp = await load_player(duel["challenger_id"])
            if cp:
                cp["coin"] += duel["coin_stake"]
                await upsert_player(duel["challenger_id"], cp)


class ChallengerMoveView(discord.ui.View):
    def __init__(self, challenger_id: int, opponent: discord.Member, stake: int):
        super().__init__(timeout=60)
        self.challenger_id = challenger_id
        self.opponent      = opponent
        self.stake         = stake

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.challenger_id:
            await interaction.response.send_message("Not your duel.", ephemeral=True)
            return False
        return True

    async def pick(self, interaction: discord.Interaction, move: str) -> None:
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
    async def attack(self, i: discord.Interaction, _: discord.ui.Button):
        await self.pick(i, "Attack")

    @discord.ui.button(label="🛡️ Defend", style=discord.ButtonStyle.primary)
    async def defend(self, i: discord.Interaction, _: discord.ui.Button):
        await self.pick(i, "Defend")

    @discord.ui.button(label="🎭 Trick", style=discord.ButtonStyle.secondary)
    async def trick(self, i: discord.Interaction, _: discord.ui.Button):
        await self.pick(i, "Trick")

    async def on_timeout(self):
        pass


# ── Quest system ───────────────────────────────────────────────────────────────
async def generate_quest(character: str, faction: str) -> dict:
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
        f"Character: {character}, Faction: {faction}. Design a quest.",
        max_tokens=600
    )
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

async def generate_quest_stage_outcome(quest_title: str, stage_desc: str,
                                        action: str, character: str, is_final: bool) -> str:
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
async def join(interaction: discord.Interaction,
               character_name: str, faction: app_commands.Choice[str]):
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
        f"Check the decree in {'<#'+str(decree_ch.id)+'>' if decree_ch else '#'+DECREE_CHANNEL} to start earning."
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="profile", description="View your character stats")
async def profile(interaction: discord.Interaction):
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

    embed = discord.Embed(title=f"⚔️ {player['character']}", color=0x4B0082)
    embed.add_field(name="Faction",  value=player["faction"],   inline=True)
    embed.add_field(name="Rank",     value=f"#{rank}",          inline=True)
    embed.add_field(name="💰 Coin",  value=str(player["coin"]), inline=True)
    embed.add_field(name="🩸 Blood", value=str(player["blood"]),inline=True)
    embed.add_field(name="Duel W/L", value=f"{player.get('duel_wins',0)}/{player.get('duel_losses',0)}", inline=True)
    if bounty:
        embed.set_footer(text=f"🎯 Bounty on your head: {bounty['amount']} Coin")
    await interaction.response.send_message(embed=embed)


@tree.command(name="decree", description="Respond to today's Daily Decree to earn Coin")
@app_commands.describe(action="What does your character do?")
async def decree_respond(interaction: discord.Interaction, action: str):
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

    player["coin"]  += 50
    player["blood"] += 10
    player["decree_responded"] = True
    await upsert_player(uid, player)
    await add_faction_score(player.get("faction"), 5)

    await interaction.followup.send(
        f"*{outcome}*\n\n**{player['character']}** earns **+50 Coin** and **+10 Blood**. 💰🩸"
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="duel", description="Challenge someone to a skill duel (50 Coin stake)")
@app_commands.describe(opponent="Who to challenge")
async def duel(interaction: discord.Interaction, opponent: discord.Member):
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
        await interaction.response.send_message(
            f"**{opponent.display_name}** hasn't joined yet. They need `/join` first.", ephemeral=True
        )
        return
    if challenger["coin"] < 50:
        await interaction.response.send_message(
            f"Need at least **50 Coin** to duel. You have **{challenger['coin']}**.", ephemeral=True
        )
        return
    if defender["coin"] < 50:
        await interaction.response.send_message(
            f"**{defender['character']}** is too broke to duel (needs 50 Coin).", ephemeral=True
        )
        return

    view = ChallengerMoveView(interaction.user.id, opponent, stake=50)
    await interaction.response.send_message(
        f"🔒 **{challenger['character']}**, pick your move — only you can see this!",
        view=view, ephemeral=True
    )


@tree.command(name="steal", description="Attempt to steal Coin from another player (30 min cooldown)")
@app_commands.describe(target="Who to rob")
async def steal(interaction: discord.Interaction, target: discord.Member):
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
        await interaction.response.send_message(
            f"**{target.display_name}** hasn't joined yet.", ephemeral=True
        )
        return
    if on_cooldown(thief.get("steal_cooldown"), 30):
        left = cooldown_left(thief["steal_cooldown"], 30)
        await interaction.response.send_message(
            f"You need to lay low for **{left}** before stealing again.", ephemeral=True
        )
        return
    if victim["coin"] < 20:
        await interaction.response.send_message(
            f"**{victim['character']}** has barely anything worth stealing.", ephemeral=True
        )
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
        narrative = "The streets of Valdris never forget what happened next." if success else "The guards were watching. A costly mistake."

    if success:
        await interaction.followup.send(
            f"🔪 **HEIST**\n\n*{narrative}*\n\n"
            f"**{cname(thief)}** pockets **{steal_amount} Coin** from **{cname(victim)}**. "
            f"(`{success_pct}%` success chance)"
        )
    else:
        await interaction.followup.send(
            f"🔪 **CAUGHT!**\n\n*{narrative}*\n\n"
            f"**{cname(thief)}** failed and paid the price. "
            f"(`{success_pct}%` success chance)"
        )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="gamble", description="Bet Coin at The Rusty Crown tavern (10 min cooldown)")
@app_commands.describe(amount="How much to bet")
async def gamble(interaction: discord.Interaction, amount: int):
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
        await interaction.response.send_message(
            f"Not enough Coin. You have **{player['coin']}**.", ephemeral=True
        )
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
    embed.add_field(name="Bet",     value=f"{amount} Coin",         inline=True)
    embed.add_field(name="Result",  value=result_line,               inline=True)
    embed.add_field(name="Balance", value=f"{player['coin']} Coin",  inline=True)
    embed.set_footer(text=f"Roll: {roll}/100  |  Win chance: {win_pct}%")
    await interaction.followup.send(embed=embed)
    await refresh_leaderboard(interaction.guild)


@tree.command(name="bounty", description="Place a bounty on someone's head")
@app_commands.describe(target="Who to mark", amount="How much Coin to offer")
async def bounty(interaction: discord.Interaction, target: discord.Member, amount: int):
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
        await interaction.response.send_message(
            f"**{target.display_name}** hasn't joined yet.", ephemeral=True
        )
        return
    if placer["coin"] < amount:
        await interaction.response.send_message(
            f"Not enough Coin. You have **{placer['coin']}**.", ephemeral=True
        )
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

    await interaction.response.send_message(
        f"🎯 **BOUNTY PLACED**\n\n"
        f"**{cname(placer)}** puts **{amount} Coin** on **{cname(victim)}'s** head.\n"
        f"Total bounty: **{new_total} Coin**\n\n"
        f"Anyone who defeats them in a duel collects automatically."
    )
    await refresh_leaderboard(interaction.guild)


@tree.command(name="bounties", description="View all active bounties")
async def bounties_list(interaction: discord.Interaction):
    bounties = await get_meta("bounties") or {}
    active   = {uid: b for uid, b in bounties.items() if b.get("amount", 0) > 0}
    if not active:
        await interaction.response.send_message("No active bounties.", ephemeral=True)
        return
    lines = ["## 🎯 Active Bounties\n"]
    players = await load_all_players()
    for uid, b in sorted(active.items(), key=lambda x: -x[1]["amount"]):
        p    = players.get(uid, {})
        name = cname(p, "Unknown")
        lines.append(f"**{name}** — **{b['amount']} Coin** (placed by {b['placed_by_name']})")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="quest", description="Begin a personal AI-generated quest")
async def quest(interaction: discord.Interaction):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)

    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if uid in active_quests:
        q = active_quests[uid]
        await interaction.response.send_message(
            f"You're already on **{q['title']}** — Stage {q['stage']+1}/3.\n"
            "Use `/questcontinue <action>` to proceed.", ephemeral=True
        )
        return

    await interaction.response.defer()
    try:
        q_data = await generate_quest(cname(player), player["faction"])
    except Exception:
        await interaction.followup.send(
            "The quest board is empty right now. Try again in a moment.", ephemeral=True
        )
        return

    reward_val = random.randint(150, 300)
    # persist quest to DB
    try:
        quest_id = await create_quest_db(uid, q_data["title"], q_data["stages"], reward_val)
    except Exception:
        # if DB fails, still keep in-memory so user can continue this session
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
    embed.add_field(name="💰 Reward", value=f"Up to {active_quests[uid]['reward']} Coin + 25 Blood", inline=False)
    embed.set_footer(text="Use /questcontinue <your action> to proceed")
    await interaction.followup.send(embed=embed)


@tree.command(name="questcontinue", description="Continue your active quest")
@app_commands.describe(action="What do you do?")
async def quest_continue(interaction: discord.Interaction, action: str):
    uid    = str(interaction.user.id)
    player = await get_or_create_player(uid, interaction.user.display_name)

    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if uid not in active_quests:
        # try load from DB
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
        await interaction.response.send_message(
            "No active quest. Use `/quest` to begin one.", ephemeral=True
        )
        return

    await interaction.response.defer()
    q         = active_quests[uid]
    stage_idx = q["stage"]
    stage     = q["stages"][stage_idx]
    is_final  = (stage_idx == 2)

    outcome = await generate_quest_stage_outcome(
        q["title"], stage["description"], action, cname(player), is_final
    )
    q["stage"] += 1
    # persist stage progress
    try:
        if q.get("id"):
            await update_quest_stage_db(q["id"], q["stage"])
    except Exception:
        pass

    if is_final:
        reward = q["reward"]
        player["coin"]  += reward
        player["blood"] += 25
        await upsert_player(uid, player)
        await add_faction_score(player.get("faction"), 20)
        # mark complete in DB if present
        try:
            if q.get("id"):
                await complete_quest_db(q["id"])
        except Exception:
            pass
        del active_quests[uid]

        embed = discord.Embed(
            title=f"✅ Quest Complete — {q['title']}",
            description=outcome,
            color=0xFFD700
        )
        embed.set_footer(text=f"Reward: +{reward} Coin, +25 Blood")
        await interaction.followup.send(embed=embed)
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


@tree.command(name="factionwar", description="View current faction war standings")
async def faction_war(interaction: discord.Interaction):
    scores = await get_meta("faction_scores") or {f: 0 for f in FACTIONS}
    since  = await get_meta("faction_week_start") or "this week"
    lines  = [f"## ⚔️ Faction War Standings\n*Since {since}*\n"]
    for f, s in sorted(scores.items(), key=lambda x: -x[1]):
        bar = "█" * max(1, s // 10) if s else "░"
        lines.append(f"**{f}**: {s} pts  {bar}")
    lines.append("\n**How to earn faction points:**")
    lines.append("  Decree response: +5  •  Duel win: +15  •  Quest complete: +20  •  Steal success: +5")
    lines.append("\n*Winner announced every Monday — winning faction earns 200 Coin each.*")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="give", description="Transfer Coin to another player")
@app_commands.describe(member="Who to give to", amount="How much")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
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
        await interaction.response.send_message(
            f"Not enough Coin. You have **{giver['coin']}**.", ephemeral=True
        )
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
async def legend(interaction: discord.Interaction, moment: str):
    await interaction.response.defer()
    lore = await ask_ai(
        "You are a chronicler for a Hall of Legends in a gritty fantasy Discord RP. "
        "Transform the moment into a dramatic third-person legend (3-4 sentences). "
        "Make it timeless and epic. No emojis.",
        f"Submitted by {interaction.user.display_name}: {moment}"
    )
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


# ── Admin commands ─────────────────────────────────────────────────────────────

@tree.command(name="forcepost", description="[Admin] Post today's decree now")
@app_commands.checks.has_permissions(administrator=True)
async def forcepost(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await post_decree(interaction.guild)
    await interaction.followup.send("Decree posted.", ephemeral=True)

@tree.command(name="forcefactionwar", description="[Admin] Resolve faction war now")
@app_commands.checks.has_permissions(administrator=True)
async def force_faction_war(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await resolve_faction_war(interaction.guild)
    await interaction.followup.send("Faction war resolved.", ephemeral=True)

@tree.command(name="addcoin", description="[Admin] Add/remove Coin")
@app_commands.describe(member="Target", amount="Amount (negative to remove)")
@app_commands.checks.has_permissions(administrator=True)
async def addcoin(interaction: discord.Interaction, member: discord.Member, amount: int):
    uid    = str(member.id)
    player = await get_or_create_player(uid, member.display_name)
    player["coin"] = max(0, player["coin"] + amount)
    await upsert_player(uid, player)
    await interaction.response.send_message(
        f"Done. {member.display_name} now has {player['coin']} Coin.", ephemeral=True
    )
    await refresh_leaderboard(interaction.guild)

@tree.command(name="resetplayer", description="[Admin] Wipe a player so they can /join again")
@app_commands.describe(member="Who to reset")
@app_commands.checks.has_permissions(administrator=True)
async def resetplayer(interaction: discord.Interaction, member: discord.Member):
    uid = str(member.id)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE uid=$1", uid)
    active_quests.pop(uid, None)
    await interaction.response.send_message(f"{member.display_name} reset.", ephemeral=True)

@tree.command(name="clearbounty", description="[Admin] Remove a bounty")
@app_commands.describe(member="Who to clear")
@app_commands.checks.has_permissions(administrator=True)
async def clearbounty(interaction: discord.Interaction, member: discord.Member):
    bounties = await get_meta("bounties") or {}
    bounties.pop(str(member.id), None)
    await set_meta("bounties", bounties)
    await interaction.response.send_message("Bounty cleared.", ephemeral=True)


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
    print(f"Blood & Coin online as {bot.user}  |  Guilds: {len(bot.guilds)}")

bot.run(TOKEN)