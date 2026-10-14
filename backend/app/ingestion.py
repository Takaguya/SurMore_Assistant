"""Background pipeline: file -> Markdown sections (with OCR) -> chunks -> NVIDIA embeddings -> Postgres.

Also re-indexes a single section after the user edits it or re-runs OCR on it.
"""

import logging

from pgvector import HalfVector

from . import db, nvidia, ocr
from .chunking import split_text
from .config import get_settings
from .converters import ExtractionError, Section, convert
from .storage import FileMissing, get_storage

logger = logging.getLogger(__name__)


MISSING_FILE = "The original file is missing from storage, so it can't be read again. Upload it again."


def _set_progress(document_id: str, message: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE documents SET progress = %s WHERE id = %s", (message, document_id))


def _chunk_rows(document: dict, section: dict) -> list[tuple[str, str | None]]:
    """(chunk text, location) pairs for one section."""
    s = get_settings()
    return [(piece, section["location"]) for piece in split_text(section["markdown"], s.chunk_size, s.chunk_overlap)]


def _embed(document: dict, chunks: list[tuple[str, str | None]], on_progress=None):
    # Prefix each chunk with where it came from; this helps retrieval for short or generic passages.
    inputs = [f"{document['filename']}{f' ({loc})' if loc else ''}\n{text}" for text, loc in chunks]
    return nvidia.embed_passages(inputs, on_progress) if inputs else []


def _insert_chunks(conn, document: dict, rows: list[tuple[str, str, str | None]], vectors) -> None:
    """rows: (section_id, text, location)"""
    model = get_settings().nvidia_embedding_model
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO chunks (id, document_id, section_id, kb_id, idx, content, location, embedding_model, embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (db.new_id(), document["id"], section_id, document["kb_id"], i, text, location, model, HalfVector(vectors[i]))
                for i, (section_id, text, location) in enumerate(rows)
            ],
        )


def _save_sections(document_id: str, sections: list[Section]) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sections WHERE document_id = %s", (document_id,))  # cascades to chunks
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO sections (id, document_id, idx, location, page, markdown, origin, notice)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (db.new_id(), document_id, i, s.location, s.page, s.markdown, s.origin, s.notice)
                    for i, s in enumerate(sections)
                ],
            )


def process_document(document_id: str, mode: str = "extract") -> None:
    """mode "extract" reads the file again (OCR included); "reindex" re-embeds the existing sections, keeping edits."""
    with db.connect() as conn:
        document = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
    if document is None:
        return  # deleted before we got to it

    progress = lambda message: _set_progress(document_id, message)  # noqa: E731
    try:
        if mode == "extract":
            progress("Reading file")
            try:
                with get_storage().local_copy(document["stored_path"]) as path:
                    sections = convert(path, document["file_type"], progress)
            except FileMissing:
                raise ExtractionError(MISSING_FILE) from None
            if not any(s.markdown for s in sections):
                raise ExtractionError("No readable text found in this file.")
            _save_sections(document_id, sections)

        with db.connect() as conn:
            sections_rows = conn.execute(
                "SELECT * FROM sections WHERE document_id = %s ORDER BY idx", (document_id,)
            ).fetchall()
        rows = [(s["id"], text, loc) for s in sections_rows for text, loc in _chunk_rows(document, s)]
        if not rows:
            raise ExtractionError("No readable text found in this file.")

        progress(f"Indexing {len(rows)} passages")
        vectors = _embed(
            document,
            [(text, loc) for _, text, loc in rows],
            lambda done, total: progress(f"Indexing passages: {done} of {total}"),
        )
        with db.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
            _insert_chunks(conn, document, rows, vectors)
            conn.execute(
                """UPDATE documents SET status = 'ready', progress = NULL, error = NULL, chunk_count = %s,
                   embedding_model = %s WHERE id = %s""",
                (len(rows), get_settings().nvidia_embedding_model, document_id),
            )
        logger.info("Indexed %s: %d sections, %d chunks", document["filename"], len(sections_rows), len(rows))
    except Exception as exc:
        if isinstance(exc, (ExtractionError, nvidia.NvidiaConfigError)):
            message = str(exc)
            logger.warning("Couldn't process %s: %s", document["filename"], message)
        else:
            logger.exception("Failed to process %s", document["filename"])
            message = nvidia.describe_api_error(exc)
        with db.connect() as conn:
            conn.execute(
                "UPDATE documents SET status = 'failed', progress = NULL, error = %s WHERE id = %s",
                (message, document_id),
            )


def update_section(section_id: str, markdown: str, *, origin: str | None = None, edited: bool = True,
                   notice: str | None = None) -> None:
    """Replace a section's Markdown and re-index just that section. Raises on embedding errors, saving nothing."""
    with db.connect() as conn:
        section = conn.execute("SELECT * FROM sections WHERE id = %s", (section_id,)).fetchone()
        document = conn.execute("SELECT * FROM documents WHERE id = %s", (section["document_id"],)).fetchone()

    section = {**section, "markdown": markdown}
    chunks = _chunk_rows(document, section)
    vectors = _embed(document, chunks)

    with db.connect() as conn:
        conn.execute(
            """UPDATE sections SET markdown = %s, edited = %s, origin = COALESCE(%s, origin), notice = %s,
               updated_at = now() WHERE id = %s""",
            (markdown, edited, origin, notice, section_id),
        )
        conn.execute("DELETE FROM chunks WHERE section_id = %s", (section_id,))
        _insert_chunks(conn, document, [(section_id, text, loc) for text, loc in chunks], vectors)
        conn.execute(
            "UPDATE documents SET chunk_count = (SELECT count(*) FROM chunks WHERE document_id = %s) WHERE id = %s",
            (document["id"], document["id"]),
        )


def ocr_section(section_id: str) -> None:
    """Run OCR again on the page (PDF) or image behind a section and replace its Markdown."""
    from PIL import Image

    with db.connect() as conn:
        section = conn.execute("SELECT * FROM sections WHERE id = %s", (section_id,)).fetchone()
        document = conn.execute("SELECT * FROM documents WHERE id = %s", (section["document_id"],)).fetchone()

    if document["file_type"] not in ("pdf", "image") or (document["file_type"] == "pdf" and not section["page"]):
        raise ExtractionError("OCR can only be re-run on PDF pages and images.")
    try:
        with get_storage().local_copy(document["stored_path"]) as path:
            if document["file_type"] == "pdf":
                image = ocr.render_pdf_page(path, section["page"])
            else:
                with Image.open(path) as opened:
                    image = opened.copy()
    except FileMissing:
        raise ExtractionError(MISSING_FILE) from None

    markdown = ocr.image_to_markdown(image)
    notice = None if markdown.strip() else "OCR found no text on this page."
    update_section(section_id, markdown, origin="ocr", edited=False, notice=notice)
