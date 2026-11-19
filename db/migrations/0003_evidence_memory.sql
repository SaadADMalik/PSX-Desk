-- Evidence store and long-term memory (Slice 3).
-- One owner per table: market-data writes evidence, memory writes memories.
-- Everyone else reads them through those services' MCP tools, never by SQL.

-- Every fact the market-data server computed, by content-hash id. Append-only:
-- the same facts are the same id, so a second insert is a no-op.
CREATE TABLE evidence (
    id         text PRIMARY KEY,
    symbol     text NOT NULL,
    kind       text NOT NULL,
    as_of      text NOT NULL,
    facts      jsonb NOT NULL,  -- "values" is a reserved word in SQL
    source     text NOT NULL,
    stale      boolean NOT NULL DEFAULT false,
    fetched_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX evidence_symbol ON evidence (symbol, as_of);

-- The desk's past conclusions, embedded for similarity search (bge-small, 384 dims).
-- No vector index yet: an exact scan is fast below ~100k rows, and exact is
-- what you want while there is little data. Add HNSW when it grows.
CREATE TABLE memories (
    id         bigserial PRIMARY KEY,
    key        text NOT NULL UNIQUE,
    symbol     text NOT NULL,
    kind       text NOT NULL,
    as_of      text NOT NULL,
    stance     text NOT NULL,
    confidence real NOT NULL,
    text       text NOT NULL,
    embedding  vector(384) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memories_symbol ON memories (symbol);

-- Append-only for their owners: SELECT and INSERT, no UPDATE, no DELETE.
GRANT USAGE ON SCHEMA public TO market_data, memory;
GRANT SELECT, INSERT ON evidence TO market_data;
GRANT SELECT, INSERT ON memories TO memory;
GRANT USAGE ON SEQUENCE memories_id_seq TO memory;
