ALTER TABLE service ADD COLUMN live_status TEXT NOT NULL DEFAULT 'unknown'
  CHECK (live_status IN ('live', 'dead', 'unknown'));
ALTER TABLE service ADD COLUMN health TEXT NOT NULL DEFAULT 'unknown'
  CHECK (health IN ('up', 'down', 'unknown'));
ALTER TABLE service ADD COLUMN last_seen TEXT;
