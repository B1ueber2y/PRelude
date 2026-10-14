CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  author TEXT,
  base_ref TEXT,
  base_sha TEXT,
  head_sha TEXT,
  merge_base TEXT,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
  progress TEXT,
  error TEXT,
  trigger_reason TEXT,
  summary TEXT,
  claude_session_id TEXT,
  worktree TEXT,
  diff TEXT,
  cost_usd REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY,
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  path TEXT NOT NULL,
  line INTEGER,
  start_line INTEGER,
  side TEXT NOT NULL DEFAULT 'RIGHT',
  in_diff INTEGER NOT NULL DEFAULT 0,
  severity TEXT NOT NULL,
  category TEXT,
  title TEXT NOT NULL,
  assessment TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '[]',
  confidence TEXT,
  claude_comment TEXT NOT NULL,
  user_draft TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | discarded
  verdict TEXT,                            -- latest Claude verdict from discussion
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                      -- user | assistant | error
  content TEXT NOT NULL,
  proposal TEXT,                           -- suggested comment wording, if any
  verdict TEXT,                            -- confirmed | revised | withdrawn
  session_id TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
  finding_id INTEGER PRIMARY KEY REFERENCES findings(id) ON DELETE CASCADE,
  session_id TEXT,
  busy INTEGER NOT NULL DEFAULT 0
);
