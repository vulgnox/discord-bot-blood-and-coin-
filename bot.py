import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import aiohttp
import random
from datetime import datetime, time
import pytz

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN        = os.environ["DISCORD_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
MODEL        = "meta-llama/llama-3.3-70b-instruct"

DECREE_CHANNEL     = os.environ.get("DECREE_CHANNEL",     "daily-decree")
LEADERBOARD_CHANNEL = os.environ.get("LEADERBOARD_CHANNEL", "leaderboard")
LORE_CHANNEL       = os.environ.get("LORE_CHANNEL",       "hall-of-legends")
DECREE_HOUR        = int(os.environ.get("DECREE_HOUR", "9"))
TIMEZONE           = os.environ.get("TIMEZONE", "Asia/Kolkata")
DATA_FILE          = "data.json"

FACTIONS = ["Shadow Hand", "Iron Crown", "The Unmarked"]

# Move system: key beats value  (Attack beats Trick, Trick beats Defend, Defend beats Attack)
BEATS = {"Attack": "Trick", "Trick": "Defend", "Defend": "Attack"}
MOVE_EMOJI = {"Attack": "⚔️", "Defend": "🛡️", "Trick": "🎭"}

# In-memory pending duels  { duel_key -> dict }
pending_duels: dict[str, dict] = {}


# ── Data helpers ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"players": {}, "decree": None, "leaderboard_msg_id": None, "lore_count": 0}
    with open(DATA_FILE) as f:
        return json.load(f)

def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_player(data: dict, uid: str, username: str) -> dict:
    """Return player record, creating a skeleton if first seen."""
    if uid not in data["players"]:
        data["players"][uid] = {
            "username": username,
            "coin": 100,
            "blood": 0,
            "faction": None,
            "character": None,
            "decree_responded": False,
        }
    else:
        data["players"][uid]["username"] = username
    return data["players"][uid]

def is_registered(player: dict) -> bool:
    """True only if the player has completed /join."""
    return bool(player.get("character") and player.get("faction"))

def display_name(player: dict, fallback: str) -> str:
    return player.get("character") or fallback


# ── AI helper ─────────────────────────────────────────────────────────────────
async def ask_ai(system: str, user: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bloodcoin-bot",
    }
    payload = {
        "model": MODEL,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
        ) as resp:
            result = await resp.json()
            return result["choices"][0]["message"]["content"].strip()


# ── Bot setup ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ── Leaderboard ───────────────────────────────────────────────────────────────
def build_leaderboard(data: dict) -> str:
    players = [p for p in data["players"].values() if is_registered(p)]
    if not players:
        return "No players yet. Use `/join` to enter the world."

    players.sort(key=lambda p: p["coin"], reverse=True)
    medals = ["👑", "⚔️", "🗡️"]
    lines = ["## 🏆  Blood & Coin — Rankings\n"]
    for i, p in enumerate(players):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(
            f"{medal} **{p['character']}** ({p['faction']})\n"
            f"  💰 {p['coin']} Coin  •  🩸 {p['blood']} Blood\n"
        )
    lines.append("\n*Updated live. Use `/decree respond` to earn today's Coin.*")
    return "\n".join(lines)

async def refresh_leaderboard(guild: discord.Guild, data: dict) -> None:
    channel = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL)
    if not channel:
        return
    content = build_leaderboard(data)
    msg_id = data.get("leaderboard_msg_id")
    try:
        if msg_id:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content=content)
            return
    except Exception:
        pass
    msg = await channel.send(content)
    data["leaderboard_msg_id"] = str(msg.id)
    save_data(data)


# ── Daily Decree ──────────────────────────────────────────────────────────────
async def generate_decree(data: dict) -> str:
    factions_active = list({p["faction"] for p in data["players"].values() if p.get("faction")})
    faction_str = ", ".join(factions_active) if factions_active else ", ".join(FACTIONS)
    system = (
        "You are the herald of a gritty fantasy city called Valdris. "
        "Write dramatic Daily Decrees for a Discord RP server. "
        "Each decree is a short event (3-5 sentences) members can react to for Coin rewards. "
        "Include: a dramatic situation, what members must DO, and a deadline flavour. "
        "Tone: dark, immersive, high stakes. No emojis. Under 120 words."
    )
    return await ask_ai(system, f"Active factions: {faction_str}. Write today's Daily Decree.")

@tasks.loop(time=time(hour=DECREE_HOUR, tzinfo=pytz.timezone(TIMEZONE)))
async def daily_decree_task():
    for guild in bot.guilds:
        await post_decree(guild)

async def post_decree(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name=DECREE_CHANNEL)
    if not channel:
        return
    data = load_data()
    for p in data["players"].values():
        p["decree_responded"] = False
    decree_text = await generate_decree(data)
    data["decree"] = {"text": decree_text, "date": str(datetime.now().date())}
    save_data(data)
    embed = discord.Embed(title="📜 The Daily Decree", description=decree_text, color=0x8B1A1A)
    embed.set_footer(text="Use /decree respond <your action> before midnight to earn 50 Coin")
    await channel.send(embed=embed)


# ── Duel system ───────────────────────────────────────────────────────────────
def resolve_moves(c_move: str, d_move: str, c_blood: int, d_blood: int) -> str:
    """
    Returns 'challenger' or 'defender'.
    Move result: 60 pts  |  Blood ratio: 40 pts  |  Tie-break: random coin flip.
    """
    # Move advantage
    if BEATS[c_move] == d_move:
        move_pts_c, move_pts_d = 60, 0
    elif BEATS[d_move] == c_move:
        move_pts_c, move_pts_d = 0, 60
    else:
        move_pts_c = move_pts_d = 30  # same move

    # Blood ratio
    total = (c_blood + d_blood) or 1
    blood_pts_c = int((c_blood / total) * 40)
    blood_pts_d = 40 - blood_pts_c

    score_c = move_pts_c + blood_pts_c
    score_d = move_pts_d + blood_pts_d

    if score_c != score_d:
        return "challenger" if score_c > score_d else "defender"
    # Perfect tie → fair coin flip
    return random.choice(["challenger", "defender"])


class DefenderMoveView(discord.ui.View):
    """Shown publicly; only the challenged player's clicks are accepted."""

    def __init__(self, duel_key: str, defender_id: int):
        super().__init__(timeout=60)
        self.duel_key    = duel_key
        self.defender_id = defender_id
        self.resolved    = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.defender_id:
            await interaction.response.send_message(
                "This duel isn't yours to answer.", ephemeral=True
            )
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

        data    = load_data()
        c_uid   = duel["challenger_id"]
        d_uid   = str(interaction.user.id)
        c_move  = duel["challenger_move"]

        challenger = get_player(data, c_uid, duel["challenger_name"])
        defender   = get_player(data, d_uid, interaction.user.display_name)

        winner = resolve_moves(c_move, d_move, challenger["blood"], defender["blood"])
        stake  = duel["coin_stake"]

        c_name = display_name(challenger, duel["challenger_name"])
        d_name = display_name(defender,   interaction.user.display_name)

        if winner == "challenger":
            challenger["coin"] += stake
            defender["coin"]    = max(0, defender["coin"] - stake)
            challenger["blood"] += 20
            winner_mention = f"<@{c_uid}>"
            winner_name    = c_name
        else:
            defender["coin"]   += stake
            challenger["coin"]  = max(0, challenger["coin"] - stake)
            defender["blood"]  += 20
            winner_mention = interaction.user.mention
            winner_name    = d_name

        save_data(data)

        # AI narration
        system = (
            "You are a battle chronicler for a gritty fantasy Discord RP. "
            "Two fighters used specific moves. Write 3 dramatic sentences describing the fight "
            "based on their moves. No emojis. Do NOT add 'Winner:' at the end."
        )
        prompt = (
            f"{c_name} (Blood {challenger['blood']}) used {c_move}.\n"
            f"{d_name} (Blood {defender['blood']}) used {d_move}.\n"
            f"{winner_name} wins. Narrate the fight."
        )
        narrative = await ask_ai(system, prompt)

        c_score = (60 if BEATS[c_move]==d_move else 0 if BEATS[d_move]==c_move else 30) + int((challenger["blood"]/(challenger["blood"]+defender["blood"] or 1))*40)
        d_score = (60 if BEATS[d_move]==c_move else 0 if BEATS[c_move]==d_move else 30) + int((defender["blood"]/(challenger["blood"]+defender["blood"] or 1))*40)

        await interaction.channel.send(
            f"## ⚔️ DUEL RESOLVED\n"
            f"<@{c_uid}> vs {interaction.user.mention}\n\n"
            f"{MOVE_EMOJI[c_move]} **{c_name}** → **{c_move}**\n"
            f"{MOVE_EMOJI[d_move]} **{d_name}** → **{d_move}**\n\n"
            f"*{narrative}*\n\n"
            f"`{c_name} {c_score}pts  vs  {d_name} {d_score}pts`\n\n"
            f"🏆 {winner_mention} wins **{stake} Coin**!"
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
        # Challenger gets their stake back on timeout


class ChallengerMoveView(discord.ui.View):
    """Ephemeral; only the challenger sees this."""

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

        # Deduct stake now so it can't be spent elsewhere
        challenger["coin"] -= self.stake
        save_data(data)

        duel_key = f"{interaction.user.id}-{self.opponent.id}-{int(datetime.now().timestamp())}"
        pending_duels[duel_key] = {
            "challenger_id":   str(interaction.user.id),
            "challenger_name": interaction.user.display_name,
            "challenger_move": move,
            "coin_stake":      self.stake,
        }

        c_name = display_name(challenger, interaction.user.display_name)
        d_name = display_name(defender,   self.opponent.display_name)

        view = DefenderMoveView(duel_key, self.opponent.id)
        await interaction.response.send_message(
            f"## ⚔️ DUEL CHALLENGE\n"
            f"**{c_name}** challenges **{d_name}**!\n\n"
            f"Stake: **{self.stake} Coin** each  •  🔒 Challenger's move sealed\n\n"
            f"{self.opponent.mention} — pick your move! You have **60 seconds** or you forfeit.",
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
        pass  # challenger didn't pick — stake not deducted yet at this point


# ── Slash commands ─────────────────────────────────────────────────────────────

@tree.command(name="join", description="Create your character and enter Valdris")
@app_commands.describe(character_name="Your character's name", faction="Pick your faction")
@app_commands.choices(faction=[app_commands.Choice(name=f, value=f) for f in FACTIONS])
async def join(interaction: discord.Interaction, character_name: str, faction: app_commands.Choice[str]):
    data   = load_data()
    uid    = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if is_registered(player):
        await interaction.response.send_message(
            f"You're already **{player['character']}** of **{player['faction']}**. "
            f"Use `/profile` to check your stats.", ephemeral=True
        )
        return

    player["character"] = character_name.strip()
    player["faction"]   = faction.value
    save_data(data)

    decree_ch = discord.utils.get(interaction.guild.text_channels, name=DECREE_CHANNEL)
    ch_mention = f"<#{decree_ch.id}>" if decree_ch else f"`#{DECREE_CHANNEL}`"

    await interaction.response.send_message(
        f"⚔️ **{character_name}** has entered Valdris, sworn to **{faction.value}**.\n"
        f"Starting purse: 💰 100 Coin  •  🩸 0 Blood\n\n"
        f"Check the decree in {ch_mention} to start earning."
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="profile", description="View your character stats")
async def profile(interaction: discord.Interaction):
    data   = load_data()
    uid    = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if not is_registered(player):
        await interaction.response.send_message("You haven't joined yet. Use `/join` first.", ephemeral=True)
        return

    registered = [p for p in data["players"].values() if is_registered(p)]
    registered.sort(key=lambda p: p["coin"], reverse=True)
    rank = next((i+1 for i, p in enumerate(registered) if p["character"] == player["character"]), "?")

    embed = discord.Embed(title=f"⚔️ {player['character']}", color=0x4B0082)
    embed.add_field(name="Faction", value=player["faction"],    inline=True)
    embed.add_field(name="Rank",    value=f"#{rank}",           inline=True)
    embed.add_field(name="💰 Coin", value=str(player["coin"]),  inline=True)
    embed.add_field(name="🩸 Blood", value=str(player["blood"]), inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="decree", description="Respond to today's Daily Decree to earn Coin")
@app_commands.describe(action="What does your character do?")
async def decree_respond(interaction: discord.Interaction, action: str):
    data   = load_data()
    uid    = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if not is_registered(player):
        await interaction.response.send_message("You need to `/join` first.", ephemeral=True)
        return
    if player.get("decree_responded"):
        await interaction.response.send_message("You already responded to today's decree.", ephemeral=True)
        return
    if not data.get("decree"):
        await interaction.response.send_message("No decree has been posted yet today.", ephemeral=True)
        return

    await interaction.response.defer()

    system = (
        "You are a dramatic fantasy narrator for a Discord RP server. "
        "A player responded to today's event. Write ONE sentence (max 20 words) "
        "describing the cinematic outcome. No emojis."
    )
    outcome = await ask_ai(system,
        f"Decree: {data['decree']['text']}\n"
        f"{player['character']} does: {action}"
    )

    player["coin"]  += 50
    player["blood"] += 10
    player["decree_responded"] = True
    save_data(data)

    await interaction.followup.send(
        f"*{outcome}*\n\n"
        f"**{player['character']}** earns **+50 Coin** and **+10 Blood**. 💰🩸"
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="duel", description="Challenge someone to a skill duel (50 Coin stake)")
@app_commands.describe(opponent="Who to challenge")
async def duel(interaction: discord.Interaction, opponent: discord.Member):
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("You can't duel yourself.", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("You can't duel a bot.", ephemeral=True)
        return

    data       = load_data()
    uid        = str(interaction.user.id)
    challenger = get_player(data, uid, interaction.user.display_name)

    if not is_registered(challenger):
        await interaction.response.send_message("You need to `/join` first.", ephemeral=True)
        return

    defender = get_player(data, str(opponent.id), opponent.display_name)
    if not is_registered(defender):
        await interaction.response.send_message(
            f"{opponent.display_name} hasn't joined the world yet. They need to use `/join` first.",
            ephemeral=True
        )
        return

    if challenger["coin"] < 50:
        await interaction.response.send_message(
            f"You need at least **50 Coin** to duel. You have **{challenger['coin']}**.", ephemeral=True
        )
        return
    if defender["coin"] < 50:
        await interaction.response.send_message(
            f"**{defender['character']}** doesn't have enough Coin to stake (needs 50).", ephemeral=True
        )
        return

    view = ChallengerMoveView(interaction.user.id, opponent, stake=50)
    await interaction.response.send_message(
        f"🔒 **{challenger['character']}**, pick your move — only you can see this!",
        view=view,
        ephemeral=True
    )


@tree.command(name="give", description="Transfer Coin to another player")
@app_commands.describe(member="Who to give to", amount="How much Coin")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("Can't give to yourself.", ephemeral=True)
        return

    data     = load_data()
    giver    = get_player(data, str(interaction.user.id), interaction.user.display_name)
    receiver = get_player(data, str(member.id),           member.display_name)

    if not is_registered(giver):
        await interaction.response.send_message("You need to `/join` first.", ephemeral=True)
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
        f"💰 **{giver['character']}** sends **{amount} Coin** to "
        f"**{display_name(receiver, member.display_name)}**."
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="legend", description="Nominate a moment for the Hall of Legends")
@app_commands.describe(moment="Describe what happened")
async def legend(interaction: discord.Interaction, moment: str):
    await interaction.response.defer()
    data = load_data()

    system = (
        "You are a chronicler writing entries for a Hall of Legends in a gritty fantasy Discord server. "
        "Transform the moment into a dramatic third-person legend (3-4 sentences). "
        "Make it timeless and epic. No emojis."
    )
    lore = await ask_ai(system, f"Submitted by {interaction.user.display_name}: {moment}")

    data["lore_count"] = data.get("lore_count", 0) + 1
    entry_num = data["lore_count"]
    save_data(data)

    embed = discord.Embed(title=f"📖 Legend #{entry_num}", description=lore, color=0xB8860B)
    embed.set_footer(text=f"Submitted by {interaction.user.display_name}")

    lore_ch = discord.utils.get(interaction.guild.text_channels, name=LORE_CHANNEL)
    if lore_ch:
        await lore_ch.send(embed=embed)
        await interaction.followup.send(f"Legend #{entry_num} inscribed in the Hall.", ephemeral=True)
    else:
        await interaction.followup.send(embed=embed)


# ── Admin commands ─────────────────────────────────────────────────────────────

@tree.command(name="forcepost", description="[Admin] Post today's decree now")
@app_commands.checks.has_permissions(administrator=True)
async def forcepost(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await post_decree(interaction.guild)
    await interaction.followup.send("Decree posted.", ephemeral=True)


@tree.command(name="addcoin", description="[Admin] Add Coin to a player")
@app_commands.describe(member="Target player", amount="Coin amount (can be negative)")
@app_commands.checks.has_permissions(administrator=True)
async def addcoin(interaction: discord.Interaction, member: discord.Member, amount: int):
    data   = load_data()
    player = get_player(data, str(member.id), member.display_name)
    player["coin"] = max(0, player["coin"] + amount)
    save_data(data)
    await interaction.response.send_message(
        f"{'Added' if amount >= 0 else 'Removed'} {abs(amount)} Coin "
        f"{'to' if amount >= 0 else 'from'} {member.display_name}. "
        f"New balance: {player['coin']}.", ephemeral=True
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="resetplayer", description="[Admin] Wipe a player's data so they can /join again")
@app_commands.describe(member="Who to reset")
@app_commands.checks.has_permissions(administrator=True)
async def resetplayer(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    uid  = str(member.id)
    if uid in data["players"]:
        del data["players"][uid]
        save_data(data)
    await interaction.response.send_message(f"{member.display_name} reset.", ephemeral=True)


# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    if not daily_decree_task.is_running():
        daily_decree_task.start()
    print(f"Blood & Coin online as {bot.user}  |  Guilds: {len(bot.guilds)}")

bot.run(TOKEN)