"""Document processing: file -> Markdown sections (with OCR) -> chunks -> NVIDIA embeddings -> Postgres.

It goes through stages that save their work as they go: read the file into sections (a PDF: one per page, with
the text stored in it), OCR the PDF pages that need it, cut the diagrams out of PDF pages, then index. A round
works until the document is ready or the round's time is up (PROCESSING_ROUND_SECONDS on serverless hosts),
and the next round picks up where it stopped. Without a time limit, one round in the background does it all.

Also re-indexes a single section after the user edits it or re-runs OCR on it.
"""

import io
import logging
import math
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from pathlib import Path

from pgvector import HalfVector

from ..core import db
from ..core.config import get_settings
from . import converters, nvidia, ocr
from .chunking import is_table_of_contents, split_text
from .converters import IMAGE_FORMATS, ExtractionError, Section
from .storage import FileMissing, get_storage

logger = logging.getLogger(__name__)


MISSING_FILE = "The original file is missing from storage, so it can't be read again. Upload it again."
FIGURE_UPLOADS_AT_ONCE = 8
FIGURE_PAGES_PER_BATCH = 20
# A new stage starts only with this much of the round left, so a stage that can't stop halfway gets a fresh round.
FRESH_STAGE_SECONDS = 120
# OCR stops starting pages this long before the round ends, so the pages in progress can finish.
OCR_WIND_DOWN_SECONDS = 60
# A round can be cut off without anyone noticing; this long after its time is up, another may take over.
LEASE_MARGIN_SECONDS = 60
# About two hours of rounds: a file that still isn't done would never be.
MAX_ROUNDS = 30


def run_round(document_id: str) -> None:
    """Work on a document until it's ready or this round's time is up. Does nothing if another round has it."""
    seconds = get_settings().processing_round_seconds
    deadline = time.monotonic() + seconds if seconds else math.inf
    lease = f"{seconds + LEASE_MARGIN_SECONDS} seconds" if seconds else "1 day"
    with db.connect() as conn:
        document = conn.execute(
            """UPDATE documents SET round_until = now() + %s::interval, rounds = rounds + 1
               WHERE id = %s AND status = 'processing' AND (round_until IS NULL OR round_until < now())
               RETURNING *""",
            (lease, document_id),
        ).fetchone()
    if document is None:
        return  # ready, failed, deleted, or another round is on it

    stage = document["stage"] or "read"
    try:
        if document["rounds"] > MAX_ROUNDS:
            raise ExtractionError("Processing this file takes too long. Try splitting it into smaller files.")
        with ExitStack() as files:
            # Fetched once for the whole round; indexing works from the saved sections alone.
            path = None if stage == "index" else files.enter_context(_local_file(document))
            while stage:
                next_stage = STAGES[stage](document, path, deadline)
                if next_stage == stage:
                    break  # out of time: the next round carries on here
                stage = next_stage
                if stage:
                    with db.connect() as conn:
                        conn.execute("UPDATE documents SET stage = %s WHERE id = %s", (stage, document_id))
                    if deadline - time.monotonic() < FRESH_STAGE_SECONDS:
                        break
    except Exception as exc:
        _fail(document, exc)
    finally:
        with db.connect() as conn:
            conn.execute("UPDATE documents SET round_until = NULL WHERE id = %s", (document_id,))


def _read(document: dict, path: Path, deadline: float) -> str:
    """The file -> sections (with the figures of anything but a PDF, whose figures are a later stage)."""
    progress = _progress(document)
    progress("Reading file")
    is_pdf = document["file_type"] == "pdf"
    if is_pdf:
        sections = converters.read_pdf_pages(path, progress)
    else:
        sections = converters.convert(path, document["file_type"], progress)
    if not is_pdf and not any(s.markdown for s in sections):
        raise ExtractionError("No readable text found in this file.")
    ocr_pages = {s.page for s in sections if converters.needs_ocr(s.markdown)} if is_pdf else set()
    _save_sections(document, sections, ocr_pages, progress)
    return "ocr" if is_pdf else "index"


def _ocr(document: dict, path: Path, deadline: float) -> str:
    """OCR the PDF pages that need it, saving each page as soon as it's read."""
    with db.connect() as conn:
        pending = {
            row["page"]: row
            for row in conn.execute(
                "SELECT id, page, markdown FROM sections WHERE document_id = %s AND ocr_pending ORDER BY idx",
                (document["id"],),
            )
        }
    left = len(pending)

    def save(page: int, result: str | Exception) -> None:
        nonlocal left
        markdown, origin, notice = converters.pdf_page_after_ocr(pending[page]["markdown"], result)
        left -= 1
        with db.connect() as conn:
            conn.execute(
                """UPDATE sections SET markdown = %s, origin = %s, notice = %s, ocr_pending = false, updated_at = now()
                   WHERE id = %s""",
                (markdown, origin, notice, pending[page]["id"]),
            )
            conn.execute(
                "UPDATE documents SET progress = %s, progress_at = now() WHERE id = %s",
                (_ocr_progress(left), document["id"]),
            )

    if pending:
        _set_progress(document["id"], _ocr_progress(left))
        ocr.ocr_pdf_pages(path, list(pending), save, stop_at=deadline - OCR_WIND_DOWN_SECONDS, give_up_at=deadline)
        if left:
            return "ocr"

    with db.connect() as conn:
        pages = conn.execute(
            "SELECT markdown, notice FROM sections WHERE document_id = %s ORDER BY idx", (document["id"],)
        ).fetchall()
    if not any(p["markdown"].strip() for p in pages):
        raise converters.pdf_without_text([p["notice"] for p in pages if p["notice"]])
    return "figures" if get_settings().extract_figures else "index"


def _ocr_progress(left: int) -> str:
    return f"Running OCR: {left} page{'' if left == 1 else 's'} left"


def _figures(document: dict, path: Path, deadline: float) -> str:
    """Cut the diagrams out of the PDF's pages, a batch of pages at a time (at least one batch per round)."""
    with db.connect() as conn:
        done = conn.execute("SELECT figures_through FROM documents WHERE id = %s", (document["id"],)).fetchone()
        section_ids = {
            row["page"]: row["id"]
            for row in conn.execute(
                "SELECT id, page FROM sections WHERE document_id = %s AND page IS NOT NULL", (document["id"],)
            )
        }
    first, total = done["figures_through"] + 1, max(section_ids, default=0)
    prefix = _figures_prefix(document)
    while first <= total:
        last = min(first + FIGURE_PAGES_PER_BATCH - 1, total)
        uploads, rows = [], []
        for page, figures in converters.pdf_figures(path, range(first, last + 1)).items():
            for k, figure in enumerate(figures):
                figure_id = db.new_id()
                stored_path = f"{prefix}/{figure_id}.webp"
                uploads.append((stored_path, figure.data))
                rows.append((figure_id, document["id"], section_ids[page], k, stored_path, figure.width, figure.height))
        _upload(uploads)
        with db.connect() as conn:
            _insert_figures(conn, rows)
            conn.execute(
                "UPDATE documents SET figures_through = %s, progress = %s, progress_at = now() WHERE id = %s",
                (last, f"Saving diagrams: page {last} of {total}", document["id"]),
            )
        first = last + 1
        if first <= total and time.monotonic() >= deadline:
            return "figures"
    return "index"


def _index(document: dict, path: Path | None, deadline: float) -> None:
    """Chunk and embed every section, replacing the document's passages; then the document is ready."""
    document_id = document["id"]
    with db.connect() as conn:
        sections_rows = conn.execute(
            "SELECT * FROM sections WHERE document_id = %s ORDER BY idx", (document_id,)
        ).fetchall()
    rows = [(s["id"], text, loc) for s in sections_rows for text, loc in _chunk_rows(document, s)]
    if not rows:
        raise ExtractionError("No readable text found in this file.")

    progress = _progress(document)
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
            """UPDATE documents SET status = 'ready', stage = NULL, progress = NULL, error = NULL, chunk_count = %s,
               embedding_model = %s WHERE id = %s""",
            (len(rows), get_settings().nvidia_embedding_model, document_id),
        )
    logger.info("Indexed %s: %d sections, %d chunks", document["filename"], len(sections_rows), len(rows))


STAGES = {"read": _read, "ocr": _ocr, "figures": _figures, "index": _index}


def _fail(document: dict, exc: Exception) -> None:
    if isinstance(exc, (ExtractionError, nvidia.NvidiaConfigError)):
        message = str(exc)
        logger.warning("Couldn't process %s: %s", document["filename"], message)
    else:
        logger.exception("Failed to process %s", document["filename"])
        message = nvidia.describe_api_error(exc)
    with db.connect() as conn:
        conn.execute(
            "UPDATE documents SET status = 'failed', stage = NULL, progress = NULL, error = %s WHERE id = %s",
            (message, document["id"]),
        )


def _set_progress(document_id: str, message: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE documents SET progress = %s, progress_at = now() WHERE id = %s", (message, document_id))


def _progress(document: dict):
    return lambda message: _set_progress(document["id"], message)


@contextmanager
def _local_file(document: dict) -> Iterator[Path]:
    try:
        with get_storage().local_copy(document["stored_path"]) as path:
            yield path
    except FileMissing:
        raise ExtractionError(MISSING_FILE) from None


def _figures_prefix(document: dict) -> str:
    return f"{document['kb_id']}/figures/{document['id']}"


def _chunk_rows(document: dict, section: dict) -> list[tuple[str, str | None]]:
    """(chunk text, location) pairs for one section. Tables of contents aren't indexed."""
    if is_table_of_contents(section["markdown"]):
        return []
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


def _insert_figures(conn, rows: list[tuple]) -> None:
    """rows: (id, document_id, section_id, idx, stored_path, width, height)"""
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO figures (id, document_id, section_id, idx, stored_path, width, height)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                rows,
            )


def _upload(uploads: list[tuple[str, bytes]], progress=None) -> None:
    """Several at a time: one by one, a book's hundreds of diagrams take minutes against a remote bucket."""
    storage = get_storage()
    with ThreadPoolExecutor(max_workers=FIGURE_UPLOADS_AT_ONCE) as pool:
        futures = [pool.submit(storage.save, path, io.BytesIO(data), len(data)) for path, data in uploads]
        for done, future in enumerate(as_completed(futures), start=1):
            future.result()
            if progress and done % 50 == 0:
                progress(f"Saving diagrams: {done} of {len(uploads)}")


def _save_sections(document: dict, sections: list[Section], ocr_pages: set[int], progress) -> None:
    """Replace the document's sections, and its figures in file storage. ocr_pages: PDF pages still to OCR."""
    prefix = _figures_prefix(document)
    get_storage().delete_prefix(prefix)  # figures from an earlier read of the file

    section_rows, figure_rows, uploads = [], [], []
    for i, s in enumerate(sections):
        section_id = db.new_id()
        section_rows.append(
            (section_id, document["id"], i, s.location, s.page, s.markdown, s.origin, s.notice, s.page in ocr_pages)
        )
        for k, figure in enumerate(s.figures):
            figure_id = db.new_id()
            stored_path = f"{prefix}/{figure_id}.webp"
            uploads.append((stored_path, figure.data))
            figure_rows.append((figure_id, document["id"], section_id, k, stored_path, figure.width, figure.height))
    _upload(uploads, progress)

    with db.connect() as conn:
        conn.execute("DELETE FROM sections WHERE document_id = %s", (document["id"],))  # cascades to chunks, figures
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO sections (id, document_id, idx, location, page, markdown, origin, notice, ocr_pending)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                section_rows,
            )
        _insert_figures(conn, figure_rows)
        conn.execute("UPDATE documents SET figures_through = 0 WHERE id = %s", (document["id"],))


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
                with Image.open(path, formats=IMAGE_FORMATS) as opened:
                    image = opened.copy()
    except FileMissing:
        raise ExtractionError(MISSING_FILE) from None

    markdown = ocr.image_to_markdown(image)
    notice = None if markdown.strip() else "OCR found no text on this page."
    update_section(section_id, markdown, origin="ocr", edited=False, notice=notice)
