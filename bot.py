import discord
from discord.ext import commands, tasks
from discord import app_commands
import json, os, aiohttp, random
from datetime import datetime, time, timedelta
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN               = os.environ["DISCORD_TOKEN"]
OPENROUTER_KEY      = os.environ["OPENROUTER_KEY"]
MODEL               = "meta-llama/llama-3.3-70b-instruct"
DECREE_CHANNEL      = os.environ.get("DECREE_CHANNEL",      "daily-decree")
LEADERBOARD_CHANNEL = os.environ.get("LEADERBOARD_CHANNEL", "leaderboard")
LORE_CHANNEL        = os.environ.get("LORE_CHANNEL",        "hall-of-legends")
DECREE_HOUR         = int(os.environ.get("DECREE_HOUR", "9"))
TIMEZONE            = os.environ.get("TIMEZONE", "Asia/Kolkata")
DATA_FILE           = "data.json"
FACTIONS            = ["Shadow Hand", "Iron Crown", "The Unmarked"]

# Duel move system: key BEATS value
BEATS      = {"Attack": "Trick", "Trick": "Defend", "Defend": "Attack"}
MOVE_EMOJI = {"Attack": "⚔️", "Defend": "🛡️", "Trick": "🎭"}

# In-memory state (reset on restart — intentional for duels/quests)
pending_duels: dict[str, dict] = {}
active_quests: dict[str, dict] = {}   # uid -> quest state


# ── Data helpers ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    default = {
        "players": {}, "decree": None, "leaderboard_msg_id": None,
        "lore_count": 0, "bounties": {},
        "faction_scores": {f: 0 for f in FACTIONS},
        "faction_week_start": str(datetime.utcnow().date()),
    }
    if not os.path.exists(DATA_FILE):
        return default
    with open(DATA_FILE) as f:
        d = json.load(f)
    for k, v in default.items():
        d.setdefault(k, v)
    return d

def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_player(data: dict, uid: str, username: str) -> dict:
    defaults = {
        "username": username, "coin": 100, "blood": 0,
        "faction": None, "character": None,
        "decree_responded": False,
        "steal_cooldown": None, "gamble_cooldown": None,
        "duel_wins": 0, "duel_losses": 0,
    }
    if uid not in data["players"]:
        data["players"][uid] = defaults
    else:
        p = data["players"][uid]
        p["username"] = username
        for k, v in defaults.items():
            p.setdefault(k, v)
    return data["players"][uid]

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

def add_faction_score(data: dict, faction: str | None, pts: int) -> None:
    if faction and faction in data["faction_scores"]:
        data["faction_scores"][faction] = data["faction_scores"].get(faction, 0) + pts


# ── AI helper ─────────────────────────────────────────────────────────────────
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
def build_leaderboard(data: dict) -> str:
    players = [(uid, p) for uid, p in data["players"].items() if is_registered(p)]
    if not players:
        return "No players yet. Use `/join` to enter the world."
    players.sort(key=lambda x: x[1]["coin"], reverse=True)
    medals = ["👑", "⚔️", "🗡️"]
    lines  = ["## 🏆  Blood & Coin — Rankings\n"]
    for i, (uid, p) in enumerate(players):
        medal      = medals[i] if i < 3 else f"`#{i+1}`"
        bounty_tag = " 🎯" if uid in data["bounties"] else ""
        w = p.get("duel_wins", 0); l = p.get("duel_losses", 0)
        lines.append(
            f"{medal} **{p['character']}** ({p['faction']}){bounty_tag}\n"
            f"  💰 {p['coin']} Coin  •  🩸 {p['blood']} Blood  •  W/L {w}/{l}\n"
        )
    lines.append("\n**⚔️ Faction War (this week)**")
    for f, s in sorted(data["faction_scores"].items(), key=lambda x: -x[1]):
        bar = "█" * max(1, s // 10)
        lines.append(f"  {f}: {s} pts  {bar}")
    lines.append("\n*🎯 = bounty on head  |  /decree respond to earn Coin*")
    return "\n".join(lines)

async def refresh_leaderboard(guild: discord.Guild, data: dict) -> None:
    ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL)
    if not ch:
        return
    content = build_leaderboard(data)
    try:
        mid = data.get("leaderboard_msg_id")
        if mid:
            msg = await ch.fetch_message(int(mid))
            await msg.edit(content=content)
            return
    except Exception:
        pass
    msg = await ch.send(content)
    data["leaderboard_msg_id"] = str(msg.id)
    save_data(data)


# ── Daily Decree ──────────────────────────────────────────────────────────────
async def generate_decree(data: dict) -> str:
    factions = list({p["faction"] for p in data["players"].values() if p.get("faction")}) or FACTIONS
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
    data = load_data()
    for p in data["players"].values():
        p["decree_responded"] = False
    text = await generate_decree(data)
    data["decree"] = {"text": text, "date": str(datetime.utcnow().date())}
    save_data(data)
    embed = discord.Embed(title="📜 The Daily Decree", description=text, color=0x8B1A1A)
    embed.set_footer(text="Use /decree respond <your action> before midnight to earn 50 Coin")
    await ch.send(embed=embed)


# ── Weekly Faction War ─────────────────────────────────────────────────────────
@tasks.loop(time=time(hour=9, tzinfo=pytz.timezone(TIMEZONE)))
async def weekly_faction_task():
    now = datetime.now(pytz.timezone(TIMEZONE))
    if now.weekday() == 0:   # Monday
        for guild in bot.guilds:
            await resolve_faction_war(guild)

async def resolve_faction_war(guild: discord.Guild) -> None:
    data   = load_data()
    scores = data["faction_scores"]
    if not any(scores.values()):
        return

    winner = max(scores, key=lambda f: scores[f])
    bonus  = 200

    # Pay all members of the winning faction
    winners = []
    for uid, p in data["players"].items():
        if is_registered(p) and p["faction"] == winner:
            p["coin"] += bonus
            winners.append(cname(p))

    # Reset scores
    data["faction_scores"]    = {f: 0 for f in FACTIONS}
    data["faction_week_start"] = str(datetime.utcnow().date())
    save_data(data)

    ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL)
    if ch and winners:
        names = ", ".join(f"**{n}**" for n in winners)
        await ch.send(
            f"## ⚔️ FACTION WAR RESULTS\n"
            f"**{winner}** dominated this week and each member earns **{bonus} Coin**!\n"
            f"Champions: {names}\n\n*New week begins now. Fight for your faction.*"
        )
    await refresh_leaderboard(guild, data)


# ── Duel system ───────────────────────────────────────────────────────────────
def resolve_moves(c_move: str, d_move: str, c_blood: int, d_blood: int) -> tuple[str, int, int]:
    """Returns ('challenger'|'defender', challenger_score, defender_score)."""
    if BEATS[c_move] == d_move:
        mp_c, mp_d = 60, 0
    elif BEATS[d_move] == c_move:
        mp_c, mp_d = 0, 60
    else:
        mp_c = mp_d = 30

    total   = (c_blood + d_blood) or 1
    bp_c    = int((c_blood / total) * 40)
    bp_d    = 40 - bp_c
    sc, sd  = mp_c + bp_c, mp_d + bp_d

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
        data       = load_data()
        c_uid      = duel["challenger_id"]
        d_uid      = str(interaction.user.id)
        c_move     = duel["challenger_move"]
        stake      = duel["coin_stake"]

        challenger = get_player(data, c_uid, duel["challenger_name"])
        defender   = get_player(data, d_uid, interaction.user.display_name)

        winner, sc, sd = resolve_moves(c_move, d_move, challenger["blood"], defender["blood"])
        c_name = cname(challenger, duel["challenger_name"])
        d_name = cname(defender,   interaction.user.display_name)

        if winner == "challenger":
            challenger["coin"]      += stake * 2   # get back own stake + win opponent's
            challenger["blood"]     += 20
            challenger["duel_wins"] += 1
            defender["duel_losses"] += 1
            add_faction_score(data, challenger.get("faction"), 15)
            winner_mention = f"<@{c_uid}>"
            winner_name    = c_name
        else:
            defender["coin"]        += stake * 2
            defender["blood"]       += 20
            defender["duel_wins"]   += 1
            challenger["duel_losses"] += 1
            add_faction_score(data, defender.get("faction"), 15)
            winner_mention = interaction.user.mention
            winner_name    = d_name

        # Check if loser had a bounty
        loser_uid   = d_uid if winner == "challenger" else c_uid
        bounty_msg  = ""
        bounty      = data["bounties"].get(loser_uid)
        if bounty and bounty.get("amount", 0) > 0:
            reward  = bounty["amount"]
            w_uid   = c_uid if winner == "challenger" else d_uid
            wp      = data["players"][w_uid]
            wp["coin"] += reward
            del data["bounties"][loser_uid]
            bounty_msg = f"\n🎯 **BOUNTY CLAIMED!** {cname(wp)} collects **{reward} Coin** for the kill!"

        save_data(data)

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
        await refresh_leaderboard(interaction.guild, data)

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
            # Refund challenger's deducted stake
            data = load_data()
            cp   = data["players"].get(duel["challenger_id"])
            if cp:
                cp["coin"] += duel["coin_stake"]
                save_data(data)


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
        data       = load_data()
        challenger = get_player(data, str(interaction.user.id), interaction.user.display_name)
        defender   = get_player(data, str(self.opponent.id),   self.opponent.display_name)

        # Lock in the stake immediately
        challenger["coin"] -= self.stake
        save_data(data)

        duel_key = f"{interaction.user.id}-{self.opponent.id}-{int(datetime.utcnow().timestamp())}"
        pending_duels[duel_key] = {
            "challenger_id":   str(interaction.user.id),
            "challenger_name": interaction.user.display_name,
            "challenger_move": move,
            "coin_stake":      self.stake,
        }

        c_name = cname(challenger, interaction.user.display_name)
        d_name = cname(defender,   self.opponent.display_name)

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
        pass  # stake not yet deducted at this point


# ── Quest system ───────────────────────────────────────────────────────────────
async def generate_quest(character: str, faction: str) -> dict:
    """Ask AI for a 3-stage quest JSON."""
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
    # Strip possible markdown fences
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

    # Validate required structure
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
    data   = load_data()
    uid    = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if is_registered(player):
        await interaction.response.send_message(
            f"You're already **{player['character']}** of **{player['faction']}**. "
            "Use `/profile` to check your stats.", ephemeral=True
        )
        return

    player["character"] = character_name.strip()
    player["faction"]   = faction.value
    save_data(data)

    decree_ch = discord.utils.get(interaction.guild.text_channels, name=DECREE_CHANNEL)
    await interaction.response.send_message(
        f"⚔️ **{character_name}** has entered Valdris, sworn to **{faction.value}**.\n"
        f"Starting purse: 💰 100 Coin  •  🩸 0 Blood\n\n"
        f"Check the decree in {'<#'+str(decree_ch.id)+'>' if decree_ch else '#'+DECREE_CHANNEL} to start earning."
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="profile", description="View your character stats")
async def profile(interaction: discord.Interaction):
    data   = load_data()
    uid    = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return

    ranked = sorted(
        [(u, p) for u, p in data["players"].items() if is_registered(p)],
        key=lambda x: x[1]["coin"], reverse=True
    )
    rank = next((i+1 for i, (u, _) in enumerate(ranked) if u == uid), "?")

    bounty = data["bounties"].get(uid)
    bounty_line = f"\n🎯 **Bounty on your head: {bounty['amount']} Coin**" if bounty else ""

    embed = discord.Embed(title=f"⚔️ {player['character']}", color=0x4B0082)
    embed.add_field(name="Faction",    value=player["faction"],               inline=True)
    embed.add_field(name="Rank",       value=f"#{rank}",                      inline=True)
    embed.add_field(name="💰 Coin",    value=str(player["coin"]),             inline=True)
    embed.add_field(name="🩸 Blood",   value=str(player["blood"]),            inline=True)
    embed.add_field(name="Duel W/L",   value=f"{player.get('duel_wins',0)}/{player.get('duel_losses',0)}", inline=True)
    if bounty_line:
        embed.set_footer(text=f"🎯 Bounty on your head: {bounty['amount']} Coin")
    await interaction.response.send_message(embed=embed)


@tree.command(name="decree", description="Respond to today's Daily Decree to earn Coin")
@app_commands.describe(action="What does your character do?")
async def decree_respond(interaction: discord.Interaction, action: str):
    data   = load_data()
    uid    = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
    if player.get("decree_responded"):
        await interaction.response.send_message("Already responded today.", ephemeral=True)
        return
    if not data.get("decree"):
        await interaction.response.send_message("No decree posted yet today.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        outcome = await ask_ai(
            "You are a dramatic fantasy narrator. A player responded to today's event. "
            "Write ONE cinematic sentence (max 20 words) describing the outcome. No emojis.",
            f"Decree: {data['decree']['text']}\n{player['character']} does: {action}"
        )
    except AIError:
        outcome = f"{player['character']} answered the call and made their mark on the city."

    player["coin"]  += 50
    player["blood"] += 10
    player["decree_responded"] = True
    add_faction_score(data, player.get("faction"), 5)
    save_data(data)

    await interaction.followup.send(
        f"*{outcome}*\n\n**{player['character']}** earns **+50 Coin** and **+10 Blood**. 💰🩸"
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="duel", description="Challenge someone to a skill duel (50 Coin stake)")
@app_commands.describe(opponent="Who to challenge")
async def duel(interaction: discord.Interaction, opponent: discord.Member):
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("Can't duel yourself.", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("Can't duel a bot.", ephemeral=True)
        return

    data       = load_data()
    uid        = str(interaction.user.id)
    challenger = get_player(data, uid, interaction.user.display_name)
    defender   = get_player(data, str(opponent.id), opponent.display_name)

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

    data   = load_data()
    uid    = str(interaction.user.id)
    thief  = get_player(data, uid, interaction.user.display_name)
    victim = get_player(data, str(target.id), target.display_name)

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

    # Blood difference affects success chance (40-70%)
    blood_diff  = thief["blood"] - victim["blood"]
    success_pct = max(30, min(70, 50 + blood_diff // 5))
    success     = random.randint(1, 100) <= success_pct

    # Steal between 10-30% of victim's Coin
    steal_amount = max(10, int(victim["coin"] * random.uniform(0.10, 0.30)))

    if success:
        steal_amount = min(steal_amount, victim["coin"])
        thief["coin"]  += steal_amount
        victim["coin"] -= steal_amount
        add_faction_score(data, thief.get("faction"), 5)
        outcome_prompt = (
            f"{cname(thief)} successfully stole {steal_amount} Coin from {cname(victim)}. "
            "Write 2 cinematic sentences about the heist. No emojis."
        )
    else:
        # Getting caught costs the thief some Blood
        penalty = min(thief["coin"], 25)
        thief["coin"]   -= penalty
        victim["blood"] += 5   # victim gets street cred for catching a thief
        outcome_prompt = (
            f"{cname(thief)} tried to rob {cname(victim)} but got caught. Lost {penalty} Coin. "
            "Write 2 cinematic sentences about getting caught. No emojis."
        )

    save_data(data)

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
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="gamble", description="Bet Coin at The Rusty Crown tavern (10 min cooldown)")
@app_commands.describe(amount="How much to bet")
async def gamble(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return

    data   = load_data()
    uid    = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

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

    # Slightly house-favoured: 45% win
    roll    = random.randint(1, 100)
    win_pct = 45
    won     = roll <= win_pct

    # Higher bets can multiply more
    if won:
        multiplier = random.choice([1.5, 2.0, 2.5])
        winnings   = int(amount * multiplier) - amount   # net gain
        player["coin"] += winnings
        result_line = f"**+{winnings} Coin** (×{multiplier})"
        colour      = 0x00FF00
    else:
        player["coin"] -= amount
        result_line = f"**-{amount} Coin**"
        colour      = 0xFF0000

    save_data(data)

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
    embed.add_field(name="Bet",    value=f"{amount} Coin",      inline=True)
    embed.add_field(name="Result", value=result_line,            inline=True)
    embed.add_field(name="Balance", value=f"{player['coin']} Coin", inline=True)
    embed.set_footer(text=f"Roll: {roll}/100  |  Win chance: {win_pct}%")
    await interaction.followup.send(embed=embed)
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="bounty", description="Place a bounty on someone's head")
@app_commands.describe(target="Who to mark", amount="How much Coin to offer")
async def bounty(interaction: discord.Interaction, target: discord.Member, amount: int):
    if target.id == interaction.user.id:
        await interaction.response.send_message("Can't bounty yourself.", ephemeral=True)
        return
    if amount < 50:
        await interaction.response.send_message("Minimum bounty is 50 Coin.", ephemeral=True)
        return

    data   = load_data()
    uid    = str(interaction.user.id)
    placer = get_player(data, uid, interaction.user.display_name)
    victim = get_player(data, str(target.id), target.display_name)

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

    # Stack bounties if one already exists
    existing = data["bounties"].get(str(target.id), {})
    new_total = existing.get("amount", 0) + amount
    placer["coin"] -= amount
    data["bounties"][str(target.id)] = {
        "amount":        new_total,
        "placed_by_uid": uid,
        "placed_by_name": cname(placer),
    }
    save_data(data)

    await interaction.response.send_message(
        f"🎯 **BOUNTY PLACED**\n\n"
        f"**{cname(placer)}** puts **{amount} Coin** on **{cname(victim)}'s** head.\n"
        f"Total bounty: **{new_total} Coin**\n\n"
        f"Anyone who defeats them in a duel collects automatically."
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="bounties", description="View all active bounties")
async def bounties_list(interaction: discord.Interaction):
    data   = load_data()
    active = {uid: b for uid, b in data["bounties"].items() if b.get("amount", 0) > 0}
    if not active:
        await interaction.response.send_message("No active bounties.", ephemeral=True)
        return
    lines = ["## 🎯 Active Bounties\n"]
    for uid, b in sorted(active.items(), key=lambda x: -x[1]["amount"]):
        p    = data["players"].get(uid, {})
        name = cname(p, "Unknown")
        lines.append(f"**{name}** — **{b['amount']} Coin** (placed by {b['placed_by_name']})")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="quest", description="Begin a personal AI-generated quest (costs 0 Coin, pays big)")
async def quest(interaction: discord.Interaction):
    data  = load_data()
    uid   = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

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

    active_quests[uid] = {
        "title":  q_data["title"],
        "stages": q_data["stages"],
        "stage":  0,
        "reward": random.randint(150, 300),
    }

    stage = q_data["stages"][0]
    embed = discord.Embed(
        title=f"🗺️ {q_data['title']}",
        description=q_data["intro"],
        color=0x2E8B57
    )
    embed.add_field(name="Stage 1/3",  value=stage["description"], inline=False)
    embed.add_field(name="Your move",  value=stage["prompt"],       inline=False)
    embed.add_field(name="💰 Reward",  value=f"Up to {active_quests[uid]['reward']} Coin + 25 Blood", inline=False)
    embed.set_footer(text="Use /questcontinue <your action> to proceed")
    await interaction.followup.send(embed=embed)


@tree.command(name="questcontinue", description="Continue your active quest")
@app_commands.describe(action="What do you do?")
async def quest_continue(interaction: discord.Interaction, action: str):
    data  = load_data()
    uid   = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if not is_registered(player):
        await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        return
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

    if is_final:
        # Quest complete
        reward = q["reward"]
        player["coin"]  += reward
        player["blood"] += 25
        add_faction_score(data, player.get("faction"), 20)
        save_data(data)
        del active_quests[uid]

        embed = discord.Embed(
            title=f"✅ Quest Complete — {q['title']}",
            description=outcome,
            color=0xFFD700
        )
        embed.set_footer(text=f"Reward: +{reward} Coin, +25 Blood")
        await interaction.followup.send(embed=embed)
        await refresh_leaderboard(interaction.guild, data)
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
    data  = load_data()
    since = data.get("faction_week_start", "this week")
    lines = [f"## ⚔️ Faction War Standings\n*Since {since}*\n"]
    for f, s in sorted(data["faction_scores"].items(), key=lambda x: -x[1]):
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
    data     = load_data()
    giver    = get_player(data, str(interaction.user.id), interaction.user.display_name)
    receiver = get_player(data, str(member.id), member.display_name)
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
    save_data(data)
    await interaction.response.send_message(
        f"💰 **{cname(giver)}** sends **{amount} Coin** to **{cname(receiver, member.display_name)}**."
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="legend", description="Nominate a moment for the Hall of Legends")
@app_commands.describe(moment="Describe what happened")
async def legend(interaction: discord.Interaction, moment: str):
    await interaction.response.defer()
    data = load_data()
    lore = await ask_ai(
        "You are a chronicler for a Hall of Legends in a gritty fantasy Discord RP. "
        "Transform the moment into a dramatic third-person legend (3-4 sentences). "
        "Make it timeless and epic. No emojis.",
        f"Submitted by {interaction.user.display_name}: {moment}"
    )
    data["lore_count"] = data.get("lore_count", 0) + 1
    entry_num = data["lore_count"]
    save_data(data)
    embed = discord.Embed(title=f"📖 Legend #{entry_num}", description=lore, color=0xB8860B)
    embed.set_footer(text=f"Submitted by {interaction.user.display_name}")
    lore_ch = discord.utils.get(interaction.guild.text_channels, name=LORE_CHANNEL)
    if lore_ch:
        await lore_ch.send(embed=embed)
        await interaction.followup.send(f"Legend #{entry_num} inscribed.", ephemeral=True)
    else:
        await interaction.followup.send(embed=embed)


# ── Admin commands ──────────────────────────────────────────────────────────────

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
    data   = load_data()
    player = get_player(data, str(member.id), member.display_name)
    player["coin"] = max(0, player["coin"] + amount)
    save_data(data)
    await interaction.response.send_message(
        f"Done. {member.display_name} now has {player['coin']} Coin.", ephemeral=True
    )
    await refresh_leaderboard(interaction.guild, data)

@tree.command(name="resetplayer", description="[Admin] Wipe a player so they can /join again")
@app_commands.describe(member="Who to reset")
@app_commands.checks.has_permissions(administrator=True)
async def resetplayer(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    uid  = str(member.id)
    if uid in data["players"]:
        del data["players"][uid]
        save_data(data)
    active_quests.pop(uid, None)
    await interaction.response.send_message(f"{member.display_name} reset.", ephemeral=True)

@tree.command(name="clearbounty", description="[Admin] Remove a bounty")
@app_commands.describe(member="Who to clear")
@app_commands.checks.has_permissions(administrator=True)
async def clearbounty(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    data["bounties"].pop(str(member.id), None)
    save_data(data)
    await interaction.response.send_message(f"Bounty cleared.", ephemeral=True)


# ── Boot ───────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    if not daily_decree_task.is_running():
        daily_decree_task.start()
    if not weekly_faction_task.is_running():
        weekly_faction_task.start()
    print(f"Blood & Coin online as {bot.user}  |  Guilds: {len(bot.guilds)}")

bot.run(TOKEN)