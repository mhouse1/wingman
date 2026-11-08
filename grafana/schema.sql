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

-- 2026-11-08 (operator): one name per jet on the jet panels. The lobby's label is
-- read by OCR (ADR 167), which spells one jet more than one way (A-6 INTRUDER and
-- A-6-INTRUDER, F-106 and F106 DELTA DART) and can cut a name short (J-20 MI), so
-- each spelling on record maps to a jet here: spellings with the same letters and
-- digits are one jet, as in wingman.jet_name.same_jet, and a spelling whose
-- letters and digits start a more common jet's, and only that jet's, is that jet
-- cut short. A jet shows its most common spelling. The panels join this view on
-- tags->>'jet'; the rows keep the spelling that was read.
CREATE OR REPLACE VIEW jet_names AS
WITH spellings AS (
    SELECT tags->>'jet' AS spelling,
           regexp_replace(upper(tags->>'jet'), '[^A-Z0-9]', '', 'g') AS letters,
           count(*) AS rounds
    FROM rounds
    WHERE tags->>'jet' IS NOT NULL
    GROUP BY 1
), jets AS (
    SELECT letters, sum(rounds) AS rounds FROM spellings GROUP BY 1
), cut_from AS (
    SELECT short.letters AS short, short.rounds AS short_rounds,
           long.letters AS long, long.rounds
    FROM jets short
    JOIN jets long ON long.letters <> short.letters
                  AND starts_with(long.letters, short.letters)
), likeliest AS (
    SELECT DISTINCT ON (short) short, short_rounds, long, rounds
    FROM cut_from
    ORDER BY short, rounds DESC, long
), cut_short AS (
    SELECT l.short, l.long
    FROM likeliest l
    JOIN cut_from c USING (short)
    WHERE l.rounds > l.short_rounds
    GROUP BY l.short, l.long
    HAVING bool_and(starts_with(l.long, c.long) OR starts_with(c.long, l.long))
), names AS (
    SELECT DISTINCT ON (letters) letters, spelling AS name
    FROM spellings
    ORDER BY letters, rounds DESC, spelling
)
SELECT s.spelling, n.name AS jet
FROM spellings s
LEFT JOIN cut_short c ON c.short = s.letters
JOIN names n ON n.letters = coalesce(c.long, s.letters);
