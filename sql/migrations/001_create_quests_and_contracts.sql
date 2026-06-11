-- Create quests table (personal and shared board quests)
CREATE TABLE IF NOT EXISTS quests (
  id SERIAL PRIMARY KEY,
  owner_uid TEXT, -- NULL for board/shared quests
  title TEXT NOT NULL,
  stages JSONB NOT NULL,
  current_stage INTEGER NOT NULL DEFAULT 0,
  reward INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', -- active, completed, cancelled
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_quests_owner_uid ON quests(owner_uid);
CREATE INDEX IF NOT EXISTS idx_quests_status ON quests(status);

-- Create contracts table for Shadow Contract mini-game
CREATE TABLE IF NOT EXISTS contracts (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  difficulty INTEGER NOT NULL,
  reward_coin INTEGER NOT NULL,
  reward_blood INTEGER NOT NULL,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_contracts_expires ON contracts(expires_at);
