import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import asyncio
import aiohttp
from datetime import datetime, time
import pytz

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.environ["DISCORD_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
MODEL = "meta-llama/llama-3.3-70b-instruct"

DECREE_CHANNEL = os.environ.get("DECREE_CHANNEL", "daily-decree")
LEADERBOARD_CHANNEL = os.environ.get("LEADERBOARD_CHANNEL", "leaderboard")
LORE_CHANNEL = os.environ.get("LORE_CHANNEL", "hall-of-legends")

DECREE_HOUR = int(os.environ.get("DECREE_HOUR", "9"))   # 9 AM
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")

DATA_FILE = "data.json"

FACTIONS = ["Shadow Hand", "Iron Crown", "The Unmarked"]

# ── Data layer ───────────────────────────────────────────────────────────────
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"players": {}, "decree": None, "leaderboard_msg_id": None, "lore_count": 0}
    with open(DATA_FILE) as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_player(data, user_id: str, username: str):
    if user_id not in data["players"]:
        data["players"][user_id] = {
            "username": username,
            "coin": 100,
            "blood": 0,
            "faction": None,
            "character": None,
            "decree_responded": False,
        }
    else:
        data["players"][user_id]["username"] = username
    return data["players"][user_id]

# ── AI helper ────────────────────────────────────────────────────────────────
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
            {"role": "user", "content": user},
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

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Leaderboard renderer ─────────────────────────────────────────────────────
def build_leaderboard(data) -> str:
    players = data["players"]
    if not players:
        return "No players yet. Use `/join` to enter the world."

    sorted_players = sorted(players.values(), key=lambda p: p["coin"], reverse=True)

    medals = ["👑", "⚔️", "🗡️"]
    lines = ["## 🏆  Blood & Coin — Rankings\n"]
    for i, p in enumerate(sorted_players):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        faction = p.get("faction") or "No faction"
        char = p.get("character") or p["username"]
        lines.append(
            f"{medal} **{char}** ({faction})\n"
            f"  💰 {p['coin']} Coin  •  🩸 {p['blood']} Blood\n"
        )

    lines.append("\n*Updated live. Use `/decree respond` to earn today's Coin.*")
    return "\n".join(lines)

async def refresh_leaderboard(guild: discord.Guild, data: dict):
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

# ── Daily Decree ─────────────────────────────────────────────────────────────
async def generate_decree(data: dict) -> str:
    players = data["players"]
    factions_active = list({p["faction"] for p in players.values() if p.get("faction")})
    faction_str = ", ".join(factions_active) if factions_active else "Shadow Hand, Iron Crown, The Unmarked"

    system = (
        "You are the herald of a gritty fantasy city called Valdris. "
        "You write dramatic, tense Daily Decrees for a Discord RP server. "
        "Each decree is a short event (3-5 sentences) that members can react to for Coin rewards. "
        "Include: a dramatic situation, what members must DO to earn Coin, and a deadline flavour. "
        "Tone: dark, immersive, high stakes. No emojis. Keep it under 120 words."
    )
    prompt = f"Active factions today: {faction_str}. Write today's Daily Decree."
    return await ask_ai(system, prompt)

@tasks.loop(time=time(hour=DECREE_HOUR, tzinfo=pytz.timezone(TIMEZONE)))
async def daily_decree_task():
    for guild in bot.guilds:
        await post_decree(guild)

async def post_decree(guild: discord.Guild):
    channel = discord.utils.get(guild.text_channels, name=DECREE_CHANNEL)
    if not channel:
        return
    data = load_data()
    # Reset daily response flags
    for p in data["players"].values():
        p["decree_responded"] = False
    decree_text = await generate_decree(data)
    data["decree"] = {"text": decree_text, "date": str(datetime.now().date())}
    save_data(data)

    embed = discord.Embed(
        title="📜 The Daily Decree",
        description=decree_text,
        color=0x8B1A1A,
    )
    embed.set_footer(text="Use /decree respond <your action> before midnight to earn 50 Coin")
    await channel.send(embed=embed)

# ── Slash commands ────────────────────────────────────────────────────────────

@tree.command(name="join", description="Create your character and join the world")
@app_commands.describe(character_name="Your character's name", faction="Pick your faction")
@app_commands.choices(faction=[
    app_commands.Choice(name=f, value=f) for f in FACTIONS
])
async def join(interaction: discord.Interaction, character_name: str, faction: app_commands.Choice[str]):
    data = load_data()
    uid = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if player.get("character"):
        await interaction.response.send_message(
            f"You're already **{player['character']}** of **{player['faction']}**. Use `/profile` to check your stats.", ephemeral=True
        )
        return

    player["character"] = character_name
    player["faction"] = faction.value
    save_data(data)

    await interaction.response.send_message(
        f"⚔️ **{character_name}** has entered Valdris, sworn to **{faction.value}**.\n"
        f"Starting purse: 💰 100 Coin  •  🩸 0 Blood\n\n"
        f"Check the decree in <#{discord.utils.get(interaction.guild.text_channels, name=DECREE_CHANNEL).id if discord.utils.get(interaction.guild.text_channels, name=DECREE_CHANNEL) else 0}> to start earning."
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="profile", description="View your character stats")
async def profile(interaction: discord.Interaction):
    data = load_data()
    uid = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    sorted_players = sorted(data["players"].values(), key=lambda p: p["coin"], reverse=True)
    rank = next((i+1 for i, p in enumerate(sorted_players) if p["username"] == player["username"]), "?")

    embed = discord.Embed(title=f"⚔️ {player.get('character') or interaction.user.display_name}", color=0x4B0082)
    embed.add_field(name="Faction", value=player.get("faction") or "None", inline=True)
    embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    embed.add_field(name="💰 Coin", value=str(player["coin"]), inline=True)
    embed.add_field(name="🩸 Blood", value=str(player["blood"]), inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="decree", description="Respond to today's Daily Decree to earn Coin")
@app_commands.describe(action="What does your character do?")
async def decree_respond(interaction: discord.Interaction, action: str):
    data = load_data()
    uid = str(interaction.user.id)
    player = get_player(data, uid, interaction.user.display_name)

    if not player.get("character"):
        await interaction.response.send_message("You need to `/join` first.", ephemeral=True)
        return

    if player.get("decree_responded"):
        await interaction.response.send_message("You already responded to today's decree.", ephemeral=True)
        return

    if not data.get("decree"):
        await interaction.response.send_message("No decree has been posted yet today.", ephemeral=True)
        return

    await interaction.response.defer()

    # AI judges the response flavour
    system = (
        "You are a dramatic fantasy narrator for a Discord RP server. "
        "A player has responded to today's event. Write ONE short sentence (max 20 words) "
        "describing the outcome of their action in a dark, cinematic way. No emojis."
    )
    prompt = f"Decree: {data['decree']['text']}\nPlayer {player['character']} does: {action}"
    outcome = await ask_ai(system, prompt)

    player["coin"] += 50
    player["blood"] += 10
    player["decree_responded"] = True
    save_data(data)

    await interaction.followup.send(
        f"*{outcome}*\n\n"
        f"**{player['character']}** earns **+50 Coin** and **+10 Blood**. 💰🩸"
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="give", description="Give Coin to another player")
@app_commands.describe(member="Who to give to", amount="How much Coin")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    data = load_data()
    giver = get_player(data, str(interaction.user.id), interaction.user.display_name)
    receiver = get_player(data, str(member.id), member.display_name)

    if giver["coin"] < amount:
        await interaction.response.send_message("Not enough Coin.", ephemeral=True)
        return

    giver["coin"] -= amount
    receiver["coin"] += amount
    save_data(data)

    await interaction.response.send_message(
        f"💰 **{giver.get('character') or interaction.user.display_name}** sends **{amount} Coin** to "
        f"**{receiver.get('character') or member.display_name}**."
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="legend", description="Nominate a moment for the Hall of Legends")
@app_commands.describe(moment="Describe the legendary moment")
async def legend(interaction: discord.Interaction, moment: str):
    await interaction.response.defer()
    data = load_data()

    system = (
        "You are a chronicler writing entries for a Hall of Legends in a gritty fantasy Discord server. "
        "Transform the submitted moment into a dramatic, third-person legend entry (3-4 sentences). "
        "Make it sound timeless and epic, like it will be remembered for ages. No emojis."
    )
    lore = await ask_ai(system, f"Moment submitted by {interaction.user.display_name}: {moment}")

    data["lore_count"] = data.get("lore_count", 0) + 1
    entry_num = data["lore_count"]
    save_data(data)

    channel = discord.utils.get(interaction.guild.text_channels, name=LORE_CHANNEL)
    embed = discord.Embed(
        title=f"📖 Legend #{entry_num}",
        description=lore,
        color=0xB8860B,
    )
    embed.set_footer(text=f"Submitted by {interaction.user.display_name}")

    if channel:
        await channel.send(embed=embed)
        await interaction.followup.send(f"Legend #{entry_num} has been inscribed in the Hall.", ephemeral=True)
    else:
        await interaction.followup.send(embed=embed)


@tree.command(name="duel", description="Challenge another player to a duel (costs 50 Coin, winner takes all)")
@app_commands.describe(opponent="Who to challenge")
async def duel(interaction: discord.Interaction, opponent: discord.Member):
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("You can't duel yourself.", ephemeral=True)
        return

    data = load_data()
    challenger = get_player(data, str(interaction.user.id), interaction.user.display_name)
    defender = get_player(data, str(opponent.id), opponent.display_name)

    if challenger["coin"] < 50:
        await interaction.response.send_message("You need at least 50 Coin to issue a duel.", ephemeral=True)
        return

    await interaction.response.defer()

    system = (
        "You are a fantasy battle narrator for a Discord RP server. "
        "Two characters are dueling. Write a short, dramatic 3-sentence battle description. "
        "Pick a winner (slightly favour the challenger). End with: 'Winner: [name]'. No emojis."
    )
    prompt = (
        f"Challenger: {challenger.get('character') or interaction.user.display_name} "
        f"(faction: {challenger.get('faction') or 'unknown'}, Blood: {challenger['blood']})\n"
        f"Defender: {defender.get('character') or opponent.display_name} "
        f"(faction: {defender.get('faction') or 'unknown'}, Blood: {defender['blood']})"
    )
    narrative = await ask_ai(system, prompt)

    # Parse winner from narrative
    challenger_name = challenger.get("character") or interaction.user.display_name
    defender_name = defender.get("character") or opponent.display_name
    challenger_wins = challenger_name.lower() in narrative.lower().split("winner:")[-1].lower()

    if challenger_wins:
        challenger["coin"] += 50
        defender["coin"] = max(0, defender["coin"] - 50)
        challenger["blood"] += 20
        winner_mention = interaction.user.mention
    else:
        defender["coin"] += 50
        challenger["coin"] = max(0, challenger["coin"] - 50)
        defender["blood"] += 20
        winner_mention = opponent.mention

    save_data(data)

    await interaction.followup.send(
        f"⚔️ **DUEL**\n{interaction.user.mention} vs {opponent.mention}\n\n"
        f"*{narrative}*\n\n"
        f"🏆 {winner_mention} wins **50 Coin**!"
    )
    await refresh_leaderboard(interaction.guild, data)


@tree.command(name="forcepost", description="[Admin] Post today's decree now")
@app_commands.checks.has_permissions(administrator=True)
async def forcepost(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await post_decree(interaction.guild)
    await interaction.followup.send("Decree posted.", ephemeral=True)


@tree.command(name="addcoin", description="[Admin] Add Coin to a player")
@app_commands.describe(member="Target player", amount="Coin amount")
@app_commands.checks.has_permissions(administrator=True)
async def addcoin(interaction: discord.Interaction, member: discord.Member, amount: int):
    data = load_data()
    player = get_player(data, str(member.id), member.display_name)
    player["coin"] += amount
    save_data(data)
    await interaction.response.send_message(f"Added {amount} Coin to {member.display_name}.", ephemeral=True)
    await refresh_leaderboard(interaction.guild, data)


# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    daily_decree_task.start()
    print(f"Blood & Coin bot online as {bot.user}")

bot.run(TOKEN)
