"""PostgreSQL + pgvector storage.

Works with any Postgres that has the pgvector extension: the local Docker container from
docker-compose.yml, or a free hosted database such as Supabase or Neon.
"""

import hashlib
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
-- Keyword search (see _search in services/rag.py) alongside search by meaning.
CREATE INDEX IF NOT EXISTS idx_chunks_keywords ON chunks USING gin (to_tsvector('english', content));

-- Diagrams and pictures cut out of a section, shown next to answers that cite it.
CREATE TABLE IF NOT EXISTS figures (
    id          TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section_id  TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    stored_path TEXT NOT NULL,                -- in file storage, under <kb_id>/figures/<document_id>/
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_figures_section ON figures(section_id, idx);

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

-- When processing last reported progress (see STALLED_AFTER).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS progress_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Processing goes through stages and can stop and pick up again (see services/ingestion.py).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS stage TEXT;                 -- read | ocr | figures | index
ALTER TABLE documents ADD COLUMN IF NOT EXISTS figures_through INTEGER NOT NULL DEFAULT 0;  -- PDF page
ALTER TABLE documents ADD COLUMN IF NOT EXISTS round_until TIMESTAMPTZ;   -- a round is working on it until then
ALTER TABLE documents ADD COLUMN IF NOT EXISTS rounds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sections ADD COLUMN IF NOT EXISTS ocr_pending BOOLEAN NOT NULL DEFAULT false;

-- Keys the server generates for itself (see _media_key in core/auth.py), shared by every server process.
CREATE TABLE IF NOT EXISTS app_secrets (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Versions of this schema that have been applied (see init_db).
CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY);

-- Supabase also serves every table in `public` through its Data API to anyone with the anon key, which the
-- browser is given. Row level security with no policies shuts that door. The app connects as the tables'
-- owner, which row level security doesn't apply to.
ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE sections ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE figures ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_version ENABLE ROW LEVEL SECURITY;
"""

# Applied only when it changes rather than on every start: it takes table locks, and serverless hosts start often.
SCHEMA_VERSION = hashlib.sha256(SCHEMA.encode()).hexdigest()[:16]

# A document that hasn't reported progress for this long counts as failed. Serverless hosts (Vercel) stop work at
# their time limit without telling anyone, and other instances may be processing while one starts up.
STALLED_AFTER = "10 minutes"
STALLED_ERROR = "Processing stopped before it finished. Click Retry."

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
            if not _schema_is_current(conn):
                conn.execute(SCHEMA)
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (%s) ON CONFLICT DO NOTHING", (SCHEMA_VERSION,)
                )
            # Processed in rounds, a document that isn't moving is waiting for its next round, not broken.
            if not settings.processing_round_seconds:
                conn.execute(
                    """UPDATE documents SET status = 'failed', progress = NULL, error = %s
                       WHERE status = 'processing' AND progress_at < now() - %s::interval""",
                    (STALLED_ERROR, STALLED_AFTER),
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


def _schema_is_current(conn: psycopg.Connection) -> bool:
    if conn.execute("SELECT to_regclass('public.schema_version')").fetchone()[0] is None:
        return False
    return conn.execute("SELECT 1 FROM schema_version WHERE version = %s", (SCHEMA_VERSION,)).fetchone() is not None


def close_db() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
