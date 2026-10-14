"""PostgreSQL + pgvector storage.

Works with any Postgres that has the pgvector extension: the local Docker container from
docker-compose.yml, or a free hosted database such as Supabase or Neon.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL DEFAULT 'local',  -- the signed-in user ("local" when sign-in is off)
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    kb_id           TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    file_type       TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    stored_path     TEXT NOT NULL,          -- relative to the upload directory
    status          TEXT NOT NULL,          -- processing | ready | failed
    progress        TEXT,                   -- what processing is doing right now
    error           TEXT,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_documents_kb ON documents(kb_id);

-- The document as editable Markdown, one row per page / slide / heading / block of rows.
CREATE TABLE IF NOT EXISTS sections (
    id          TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    location    TEXT,                       -- "Page 3", "Slide 5: Intro", "Sheet Sales, rows 2-40"
    page        INTEGER,                    -- PDF page number, so the page can be OCR'd again
    markdown    TEXT NOT NULL,
    origin      TEXT NOT NULL,              -- text (read from the file) | ocr
    edited      BOOLEAN NOT NULL DEFAULT false,
    notice      TEXT,                       -- e.g. why OCR didn't work on this page
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sections_document ON sections(document_id, idx);

-- Searchable pieces of sections. halfvec stores 2 bytes per dimension, halving database size.
CREATE TABLE IF NOT EXISTS chunks (
    id              TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section_id      TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    kb_id           TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    content         TEXT NOT NULL,
    location        TEXT,
    embedding_model TEXT NOT NULL,
    embedding       halfvec NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_kb ON chunks(kb_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section_id);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL DEFAULT 'local',
    title      TEXT NOT NULL,
    kb_ids     JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    sources         JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);

-- Databases created before sign-in existed: everything in them belongs to "local".
ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT 'local';
CREATE INDEX IF NOT EXISTS idx_knowledge_bases_owner ON knowledge_bases(owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_id, updated_at);
"""

_pool: ConnectionPool | None = None


def new_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """A pooled connection; the transaction commits on success and rolls back on error."""
    if _pool is None:
        raise RuntimeError("Database not initialised")
    with _pool.connection() as conn:
        yield conn


def init_db() -> None:
    global _pool
    settings = get_settings()
    if settings.storage_backend == "local":
        settings.upload_dir.mkdir(parents=True, exist_ok=True)

    try:
        # The extension must exist before pgvector types can be registered on pooled connections.
        with psycopg.connect(settings.database_url, autocommit=True, prepare_threshold=None) as conn:
            conn.execute(SCHEMA)
            # Anything still "processing" was interrupted by a server restart.
            conn.execute(
                "UPDATE documents SET status = 'failed', progress = NULL, error = %s WHERE status = 'processing'",
                ("Processing was interrupted because the server restarted. Click Retry.",),
            )
    except psycopg.OperationalError as exc:
        raise RuntimeError(
            f"Couldn't connect to PostgreSQL at DATABASE_URL. Start the database "
            f"(`docker compose up -d` in the project folder) or check the URL in backend/.env.\n{exc}"
        ) from exc

    _pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=10,
        # prepare_threshold=None keeps this working behind poolers such as Supabase's Supavisor.
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
        configure=register_vector,
        open=True,
    )


def close_db() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
