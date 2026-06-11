#!/usr/bin/env python3
"""Smoke test for DB migrations and helpers.

Usage:
  export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
  python3 scripts/smoke_test.py

This script sets minimal dummy env vars for DISCORD_TOKEN and OPENROUTER_KEY
if they're not present so `bot.py` can be imported. It requires a valid
`DATABASE_URL` pointing at a test or dev Postgres instance.
"""
import os
import asyncio

# Provide dummy values for required env vars so importing bot.py won't fail
os.environ.setdefault("DISCORD_TOKEN", "dummy_token_for_tests")
os.environ.setdefault("OPENROUTER_KEY", "dummy_openrouter_key")

from bot import (
    init_db, create_contract_db, fetch_active_contracts,
    create_quest_db, fetch_active_quest_by_owner, load_active_quests_from_db
)


async def main():
    print("Initializing DB pool and ensuring tables...")
    await init_db()
    print("Loading active quests into memory (if any)...")
    await load_active_quests_from_db()

    print("Creating test contract...")
    cid = await create_contract_db("Test Contract", difficulty=2, reward_coin=100, reward_blood=5, metadata={})
    print(f"Created contract id: {cid}")

    rows = await fetch_active_contracts()
    print("Active contracts:")
    for r in rows:
        print(r)

    print("Creating test quest for owner 'tester'...")
    stages = [{"description": "Stage 1", "prompt": "Do X"}, {"description": "Stage 2", "prompt": "Do Y"}, {"description": "Stage 3", "prompt": "Do Z"}]
    qid = await create_quest_db("tester", "Test Quest", stages, reward=200)
    print(f"Created quest id: {qid}")
    q = await fetch_active_quest_by_owner("tester")
    print("Fetched quest for tester:")
    print(q)


if __name__ == "__main__":
    asyncio.run(main())
