# CHANGELOG

All notable changes for Blood & Coin.

The format is based on semantic versioning, with each release summarizing major features, gameplay systems, and stability improvements.

---

## [v1.0.0] - Initial launch
### Added
- Core Discord bot built with `discord.py` and slash command support.
- Persistent PostgreSQL-backed player database with `players`, `meta`, `quests`, and `contracts` tables.
- Daily leaderboard system posted/updated in `#leaderboard`.
- `/join` to create a character, choose a faction, and start with 100 Coin.
- `/profile` to view Coin, Blood, rank, duel record, and active bounty status.
- `/decree respond` for daily ritual gameplay that rewards Coin and Blood.
- AI-powered Daily Decree generation using OpenRouter + `meta-llama/llama-3.3-70b-instruct`.
- Automatic leaderboard refresh after player-facing actions.
- Basic admin ops to post decrees manually and award Coin.

### Improvements
- Added fallback handling for AI request failures so gameplay still continues when the model is unavailable.
- Designed the bot to run in timezone-aware mode with `DECREE_HOUR` and `TIMEZONE` environment configuration.

---

## [v1.1.0] - Duel economy & skill system
### Added
- `/duel @user` to issue a 50 Coin challenge with sealed challenger move selection.
- Public defender selection via buttons: Attack, Defend, Trick.
- Rock-paper-scissors ladder with Blood-based power scaling.
- Duel stakes are locked up front and returned if the defender fails to choose before timeout.
- Duel win rewards: stake payout, Blood bonus, faction points, and automatic bounty collection.
- Live duel narration from AI with a dramatic fight summary.

### Fixes
- Ensured only the challenged defender can choose a move.
- Prevented self-duels, bot duels, and duels against unregistered players.
- Protected the duel flow from invalid or stale pending challenges.

---

## [v1.2.0] - Risk, reward, and player-driven conflict
### Added
- `/steal @user` with a 30-minute cooldown, chance based on relative Blood.
- Successful heists steal 10–30% of the target's Coin.
- Failed thefts cost Coin and grant Blood to the victim.
- `/gamble <amount>` at The Rusty Crown with a 10-minute cooldown and house-edge win chance.
- Dynamic gamble payouts with 1.5x, 2x, or 2.5x rewards and dice-roll narration.
- `/bounty @user <amount>` to place stackable bounties, deducted upfront.
- `/bounties` to view all active marks.

### Improvements
- Added better cooldown messaging and validation for negative or impossible actions.
- Integrated bounty state into the leaderboard display.

---

## [v1.3.0] - AI quests, Shadow Contracts, and immersive progression
### Added
- `/quest` to generate a personal 3-stage quest tailored to a players character and faction.
- `/questcontinue <action>` to advance through quest stages with AI-generated outcomes.
- Quest reward structure: 150–300 Coin and +25 Blood for completion.
- Persistent quest storage to save active quest state in the database.
- `/contracts` to list active Shadow Contracts.
- `/acceptcontract <id>` to attempt contracts with success chances influenced by difficulty, Blood, and player power.
- Contract rewards for Coin and Blood, and automatic contract expiration after attempt.

### Improvements
- Added robust quest JSON validation to reject malformed AI responses.
- Added fallback messages if quest generation or narration fails.

---

## [v1.4.0] - Faction war, lore, and server culture
### Added
- `/factionwar` to view current faction standings, earn points, and see weekly progress.
- Weekly faction war resolution every Monday at 9 AM server time.
- Winning faction members receive a 200 Coin bonus.
- `/legend` to submit memorable moments and turn them into Hall of Legends lore.
- Lore is posted in `#hall-of-legends` with sequential legend numbering.
- Daily Decree resets players' decree response state automatically when a new decree posts.

### Improvements
- Added faction score persistence and weekly reset logic.
- Improved leaderboard formatting with faction war bars and bounty indicators.

---

## [v1.5.0] - Stability, admin tools, and operational polish
### Added
- `/addcoin` admin command with positive/negative values for flexible economy adjustments.
- `/addcontract` admin command to create Shadow Contracts from Discord.
- `/resetplayer` admin command to wipe a player record cleanly.
- `/clearbounty` admin command to remove unwanted bounty entries.
- Automatic database initialization and migration-safe table creation on startup.
- Startup state recovery for active quests from the database.

### Improvements
- Added AI request timeout and HTTP status validation to prevent blocked requests.
- AI helpers now raise clear `AIError` exceptions.
- Added more robust `get_or_create_player()` updates for username changes.
- Improved leaderboard message editing by stored message ID with fallback to new posts.
- Ensured every action that affects Coin/Blood refreshes the leaderboard.

---

## Planned
- Add persistent player metadata for richer progression systems.
- Expand quest variety, guild objectives, and seasonal events.
- Add richer lore mechanics and player-driven story arcs.
- Add tests and monitoring for higher confidence in bot stability.
