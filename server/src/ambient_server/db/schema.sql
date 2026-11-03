-- Ambient Memory L1 store schema (v1).
-- Bi-temporal, provenance-first: memories are never overwritten, only closed
-- (valid_to/status) and superseded. Every memory links to source utterances.

CREATE TABLE IF NOT EXISTS speakers (
    id            TEXT PRIMARY KEY,              -- client-generated uuid
    label         TEXT NOT NULL,                 -- "Speaker-3" | "Jasmine" | "Owner"
    name          TEXT,                          -- resolved name once known
    is_owner      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'provisional',  -- provisional|named|merged
    merged_into   TEXT REFERENCES speakers(id),
    first_seen    TEXT,
    last_seen     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id                     TEXT PRIMARY KEY,
    started_at             TEXT NOT NULL,
    last_utterance_at      TEXT NOT NULL,
    closed_at              TEXT,                 -- NULL = open
    close_reason           TEXT,                 -- silence|location|manual
    location_lat           REAL,
    location_lon           REAL,
    location_accuracy_m    REAL,
    utterance_count        INTEGER NOT NULL DEFAULT 0,
    extraction_status      TEXT NOT NULL DEFAULT 'none',  -- none|incremental|extracted|error
    last_extracted_utterance_id TEXT,
    summary                TEXT,
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_open ON conversations(closed_at) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS utterances (
    id                 TEXT PRIMARY KEY,          -- client uuid; idempotency key
    conversation_id    TEXT REFERENCES conversations(id),
    revision           INTEGER NOT NULL DEFAULT 0,
    start_ts           TEXT NOT NULL,             -- ISO8601 with offset
    end_ts             TEXT,
    speaker_id         TEXT REFERENCES speakers(id),
    speaker_label      TEXT,
    speaker_confidence REAL,
    text               TEXT NOT NULL,
    asr_confidence     REAL,
    is_whisper         INTEGER NOT NULL DEFAULT 0,
    location_json      TEXT,
    device             TEXT,
    received_at        TEXT NOT NULL,
    extracted          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_utterances_conversation ON utterances(conversation_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_utterances_unextracted ON utterances(extracted) WHERE extracted = 0;

CREATE TABLE IF NOT EXISTS utterance_revisions (
    utterance_id  TEXT NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    revision      INTEGER NOT NULL,
    text          TEXT NOT NULL,
    speaker_id    TEXT,
    speaker_label TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (utterance_id, revision)
);

CREATE TABLE IF NOT EXISTS entities (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,                -- person|place|project|org|acronym|other
    name       TEXT NOT NULL,                -- canonical surface form
    aliases    TEXT NOT NULL DEFAULT '[]',   -- JSON array
    unresolved INTEGER NOT NULL DEFAULT 0,   -- 1 = flagged for clarification
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS memories (
    id                   TEXT PRIMARY KEY,
    text                 TEXT NOT NULL,
    structured_json      TEXT NOT NULL,       -- full extractor payload (who_said, entities,
                                              -- time_phrase + resolved ISO, place, ...)
    type                 TEXT NOT NULL,       -- event|commitment|preference|plan|personal_fact|
                                              -- opinion|question|other
    importance           REAL NOT NULL DEFAULT 0.5,
    confidence           REAL NOT NULL DEFAULT 0.5,
    event_start          TEXT,                -- resolved world-time of the described event
    event_end            TEXT,
    valid_from           TEXT NOT NULL,       -- when the fact began to hold
    valid_to             TEXT,                -- NULL = current
    recorded_at          TEXT NOT NULL,
    source_utterance_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array (provenance)
    speaker_id           TEXT REFERENCES speakers(id),
    conversation_id      TEXT REFERENCES conversations(id),
    status               TEXT NOT NULL DEFAULT 'active',  -- active|superseded|retracted
    superseded_by        TEXT REFERENCES memories(id),
    source               TEXT NOT NULL DEFAULT 'extraction',  -- extraction|user_clarification|lookup_pinned
    embedding            BLOB                 -- float32 little-endian
);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_speaker ON memories(speaker_id);
CREATE INDEX IF NOT EXISTS idx_memories_event_start ON memories(event_start);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'about',  -- about|said_by|located_at|recipient
    PRIMARY KEY (memory_id, entity_id, role)
);
CREATE INDEX IF NOT EXISTS idx_memory_entities_entity ON memory_entities(entity_id);

-- Simple temporal graph edges between entities (co-occurrence, LLM-asserted relations).
CREATE TABLE IF NOT EXISTS entity_edges (
    src        TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst        TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src, dst, kind)
);

-- Rolling per-entity synthesis (L2 "observation" analog; async deep-LLM regeneration).
CREATE TABLE IF NOT EXISTS entity_observations (
    entity_id   TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    source_memory_count INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clarifications (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,   -- speaker_identity|unknown_entity|ambiguous_ref|
                                     -- low_confidence_fact|conflict
    question        TEXT NOT NULL,
    context_json    TEXT NOT NULL DEFAULT '{}',
    importance      REAL NOT NULL DEFAULT 0.5,
    uncertainty     REAL NOT NULL DEFAULT 0.5,
    expected_value  REAL NOT NULL DEFAULT 0.25,  -- importance * uncertainty (ranked)
    status          TEXT NOT NULL DEFAULT 'open',  -- open|reminded|answered|expired
    conversation_id TEXT REFERENCES conversations(id),
    related_memory_ids   TEXT NOT NULL DEFAULT '[]',
    related_speaker_id   TEXT,
    related_entity_id    TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    reminded        INTEGER NOT NULL DEFAULT 0,
    digest_sent_at  TEXT,
    answered_at     TEXT,
    answer_text     TEXT,
    answer_source   TEXT             -- text|voice
);
CREATE INDEX IF NOT EXISTS idx_clarifications_open ON clarifications(status) WHERE status IN ('open','reminded');

CREATE TABLE IF NOT EXISTS cards_shown (
    id                    TEXT PRIMARY KEY,
    memory_id             TEXT REFERENCES memories(id),
    kind                  TEXT NOT NULL DEFAULT 'relevance',  -- relevance|lookup|query
    score                 REAL NOT NULL DEFAULT 0,
    features_json         TEXT NOT NULL DEFAULT '{}',         -- all score features (spec §5.4.7)
    context_utterance_id  TEXT,
    shown_at              TEXT NOT NULL,
    dismissed_at          TEXT,
    pinned_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_shown_memory ON cards_shown(memory_id, shown_at);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    TEXT NOT NULL REFERENCES cards_shown(id) ON DELETE CASCADE,
    memory_id  TEXT,
    kind       TEXT NOT NULL,    -- dismiss|thumb_up|thumb_down|pin|unpin|expand
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_memory ON feedback(memory_id, kind);

CREATE TABLE IF NOT EXISTS vocabulary (
    term        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'name',   -- name|acronym|place|other
    definition  TEXT,
    entity_id   TEXT REFERENCES entities(id),
    boost       REAL NOT NULL DEFAULT 5.0,
    source      TEXT NOT NULL DEFAULT 'manual', -- clarification|manual
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Key/value for internal bookkeeping (last digest, schema notes...).
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Full-text (porter stemmer). Kept in sync by store code (insert/delete).
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5(
    utterance_id UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);

-- Brute-force vector fallback (used when the sqlite-vec extension is unavailable).
CREATE TABLE IF NOT EXISTS memory_vectors_fb (
    memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS utterance_vectors_fb (
    utterance_id TEXT PRIMARY KEY REFERENCES utterances(id) ON DELETE CASCADE,
    embedding    BLOB NOT NULL
);
