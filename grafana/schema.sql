-- Design 018: the score dashboard's tables. scripts/round-scores.py applies this
-- before every load, so it must stay idempotent: a later column arrives as
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS, never as an edit of a CREATE.

CREATE TABLE IF NOT EXISTS rounds (
    run_id          text        NOT NULL,  -- the session, e.g. 20261028_004906_acct1
    mission_index   integer     NOT NULL,  -- the mission's index, or the reading's order in a log
    started_at      timestamptz,           -- NULL for a round from a log
    ended_at        timestamptz NOT NULL,  -- the time axis
    score           integer,               -- NULL: the panel was not read
    kills           integer,
    deaths          integer,
    assists         integer,
    wingman_version text,
    tags            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, mission_index)
);
CREATE INDEX IF NOT EXISTS rounds_ended_at ON rounds (ended_at);
CREATE INDEX IF NOT EXISTS rounds_tags ON rounds USING gin (tags);
-- 2026-10-29 (operator): a round with a manual takeover is kept, with its count,
-- and charted apart from wingman's own rounds.
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS manual_takeovers integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS changes (         -- loaded from grafana/changes.yaml
    at    timestamptz NOT NULL,               -- the start of the first session that flew it
    label text        NOT NULL,
    ref   text,                               -- the ADR or document behind it
    PRIMARY KEY (at, label)
);

CREATE TABLE IF NOT EXISTS squadron_rankings (  -- Phase 3, ADR 166
    run_id  text        NOT NULL,                -- the session that read it
    read_at timestamptz NOT NULL,
    rank    integer     NOT NULL,                -- the squadron's place on the leaderboard
    tags    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, read_at)
);
-- 2026-10-31 (operator): the squadron's trophy count, read on the same row.
ALTER TABLE squadron_rankings ADD COLUMN IF NOT EXISTS trophies bigint;
