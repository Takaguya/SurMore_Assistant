import mimetypes
import re
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile

from ..core import db, limits
from ..core.auth import User, current_user, media_user
from ..core.config import get_settings
from ..schemas import (
    Document,
    Figure,
    ProcessingDocument,
    RejectedFile,
    Section,
    SectionUpdate,
    UploadDone,
    UploadLink,
    UploadResult,
    UploadStart,
)
from ..services import ingestion, nvidia
from ..services.converters import ExtractionError, detect_file_type
from ..services.rag import figures_by_section
from ..services.storage import FileMissing, FileTooLarge, get_storage
from .knowledge_bases import get_kb_or_404

router = APIRouter(prefix="/api", tags=["documents"])

# Only documents in knowledge bases the user owns are visible.
_OWNED = "d.kb_id IN (SELECT id FROM knowledge_bases WHERE owner_id = %s)"

_SELECT_DOCUMENT = f"""
SELECT d.*,
       (SELECT count(*) FROM sections s WHERE s.document_id = d.id) AS section_count,
       (SELECT count(*) FROM sections s WHERE s.document_id = d.id AND s.origin = 'ocr') AS ocr_sections,
       (SELECT count(*) FROM sections s WHERE s.document_id = d.id AND s.edited) AS edited_sections,
       d.status = 'processing' AND d.progress_at < now() - interval '{db.STALLED_AFTER}' AS stalled,
       coalesce(d.round_until > now(), false) AS busy
FROM documents d
"""


def _to_model(row: dict) -> Document:
    data = dict(row)
    data.pop("stored_path")
    data.pop("busy")
    # Processed in rounds, a document that isn't moving is waiting for the browser to start its next round.
    if data.pop("stalled") and not get_settings().processing_round_seconds:
        data.update(status="failed", progress=None, error=db.STALLED_ERROR)
    data["needs_reindex"] = (
        data["status"] == "ready" and data["embedding_model"] != get_settings().nvidia_embedding_model
    )
    return Document(**data)


def _get_doc_or_404(conn: psycopg.Connection, document_id: str, user: User) -> dict:
    row = conn.execute(_SELECT_DOCUMENT + f" WHERE d.id = %s AND {_OWNED}", (document_id, user.id)).fetchone()
    if row is None:
        raise HTTPException(404, "Document not found")
    return row


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[Document])
def list_documents(kb_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        get_kb_or_404(conn, kb_id, user)
        rows = conn.execute(_SELECT_DOCUMENT + " WHERE d.kb_id = %s ORDER BY d.created_at DESC", (kb_id,)).fetchall()
    return [_to_model(r) for r in rows]


@router.post("/knowledge-bases/{kb_id}/documents", response_model=UploadResult, status_code=201)
def upload_documents(
    kb_id: str, background: BackgroundTasks, files: list[UploadFile] = File(...), user: User = Depends(current_user)
):
    settings = get_settings()
    storage = get_storage()
    with db.connect() as conn:
        get_kb_or_404(conn, kb_id, user)
        limits.check_upload(conn, user.id, len(files))
        space_left = limits.storage_left_bytes(conn, user.id)

    accepted: list[str] = []
    rejected: list[RejectedFile] = []

    for upload in files:
        filename = Path(upload.filename or "untitled").name
        try:
            file_type = detect_file_type(filename)
        except ExtractionError as exc:
            rejected.append(RejectedFile(filename=filename, reason=str(exc)))
            continue

        doc_id = db.new_id()
        stored_path = _stored_path(kb_id, doc_id, filename)
        max_bytes = settings.max_upload_mb * 1024 * 1024
        out_of_space = space_left is not None and space_left < max_bytes
        try:
            size = storage.save(stored_path, upload.file, space_left if out_of_space else max_bytes)
        except FileTooLarge:
            reason = (
                f"Not enough space left in your account ({settings.limit_storage_mb_per_user} MB limit)."
                if out_of_space
                else f"File is larger than {settings.max_upload_mb} MB."
            )
            rejected.append(RejectedFile(filename=filename, reason=reason))
            continue
        if size == 0:
            storage.delete(stored_path)
            rejected.append(RejectedFile(filename=filename, reason="File is empty."))
            continue
        if space_left is not None:
            space_left -= size

        with db.connect() as conn:
            conn.execute(
                """INSERT INTO documents (id, kb_id, filename, file_type, size_bytes, stored_path, status, progress, stage)
                   VALUES (%s, %s, %s, %s, %s, %s, 'processing', 'Waiting to start', 'read')""",
                (doc_id, kb_id, filename, file_type, size, stored_path),
            )
        _start_processing(background, doc_id)
        accepted.append(doc_id)

    with db.connect() as conn:
        rows = [_get_doc_or_404(conn, doc_id, user) for doc_id in accepted]
    return UploadResult(documents=[_to_model(r) for r in rows], rejected=rejected)


# Direct uploads (STORAGE_BACKEND=s3): the browser sends the file straight to the bucket, so it never passes
# through this server or its host's request size limit (4.5 MB on Vercel). Two steps: get a link for the file,
# then report it as arrived.


@router.post("/knowledge-bases/{kb_id}/uploads", response_model=UploadLink)
def start_upload(kb_id: str, body: UploadStart, user: User = Depends(current_user)):
    _require_direct_uploads()
    filename = _upload_filename(body.filename)
    with db.connect() as conn:
        get_kb_or_404(conn, kb_id, user)
        _check_upload_size(body.size, limits.storage_left_bytes(conn, user.id))
        limits.check_upload(conn, user.id, 1)
    doc_id = db.new_id()
    url = get_storage().upload_url(_stored_path(kb_id, doc_id, filename), body.size)
    return UploadLink(url=url, document_id=doc_id)


@router.post("/knowledge-bases/{kb_id}/uploads/{document_id}", response_model=Document, status_code=201)
def finish_upload(
    kb_id: str, document_id: str, body: UploadDone, background: BackgroundTasks, user: User = Depends(current_user)
):
    _require_direct_uploads()
    if not re.fullmatch(r"[0-9a-f]{32}", document_id):
        raise HTTPException(404, "Upload not found")
    filename = _upload_filename(body.filename)
    stored_path = _stored_path(kb_id, document_id, filename)
    storage = get_storage()
    with db.connect() as conn:
        get_kb_or_404(conn, kb_id, user)
        space_left = limits.storage_left_bytes(conn, user.id)
    try:
        size = storage.size(stored_path)
    except FileMissing:
        raise HTTPException(400, f"{filename} didn't finish uploading. Try again.") from None
    try:
        _check_upload_size(size, space_left)
    except HTTPException:
        storage.delete(stored_path)
        raise

    with db.connect() as conn:
        added = conn.execute(
            """INSERT INTO documents (id, kb_id, filename, file_type, size_bytes, stored_path, status, progress, stage)
               VALUES (%s, %s, %s, %s, %s, %s, 'processing', 'Waiting to start', 'read')
               ON CONFLICT (id) DO NOTHING RETURNING id""",
            (document_id, kb_id, filename, detect_file_type(filename), size, stored_path),
        ).fetchone()
        row = _get_doc_or_404(conn, document_id, user)
    if added:  # not when the same upload is reported twice
        _start_processing(background, document_id)
    return _to_model(row)


def _require_direct_uploads() -> None:
    if get_settings().storage_backend != "s3":
        raise HTTPException(400, "Direct uploads need STORAGE_BACKEND=s3.")


def _upload_filename(name: str) -> str:
    filename = Path(name).name
    try:
        detect_file_type(filename)
    except ExtractionError as exc:
        raise HTTPException(400, str(exc)) from None
    return filename


def _check_upload_size(size: int, space_left: int | None) -> None:
    settings = get_settings()
    if size == 0:
        raise HTTPException(400, "File is empty.")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
    if space_left is not None and size > space_left:
        raise HTTPException(
            413, f"Not enough space left in your account ({settings.limit_storage_mb_per_user} MB limit)."
        )


def _stored_path(kb_id: str, document_id: str, filename: str) -> str:
    # Stored under a generated name; the original filename only lives in the database.
    return f"{kb_id}/{document_id}{Path(filename).suffix.lower()}"


@router.get("/documents/{document_id}", response_model=Document)
def get_document(document_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        return _to_model(_get_doc_or_404(conn, document_id, user))


@router.post("/documents/{document_id}/reprocess", response_model=Document)
def reprocess_document(
    document_id: str,
    background: BackgroundTasks,
    mode: Literal["auto", "extract", "reindex"] = "auto",
    user: User = Depends(current_user),
):
    """extract: read the file again (replaces edits). reindex: re-embed the current Markdown.
    auto: reindex when the document already has sections, otherwise extract."""
    with db.connect() as conn:
        row = _get_doc_or_404(conn, document_id, user)
        if row["status"] == "processing" and not (row["stalled"] and not row["busy"]):
            raise HTTPException(409, "This document is already being processed")
        if mode == "auto":
            mode = "reindex" if row["section_count"] else "extract"
        if mode == "reindex" and not row["section_count"]:
            raise HTTPException(400, "This document has no text to index yet. Read the file again instead.")
        conn.execute(
            """UPDATE documents SET status = 'processing', progress = 'Waiting to start', progress_at = now(),
               error = NULL, stage = %s, rounds = 0, round_until = NULL WHERE id = %s""",
            ("read" if mode == "extract" else "index", document_id),
        )
        row = _get_doc_or_404(conn, document_id, user)
    _start_processing(background, document_id)
    return _to_model(row)


def _start_processing(background: BackgroundTasks, document_id: str) -> None:
    """Without a time limit, the whole job runs in the background now. On serverless hosts the browser runs it
    in rounds instead (process_round), since work left running after a response can be cut off there."""
    if not get_settings().processing_round_seconds:
        background.add_task(ingestion.run_round, document_id)


@router.get("/processing", response_model=list[ProcessingDocument])
def list_processing(user: User = Depends(current_user)):
    """The user's documents still being processed, for the browser to run their next rounds."""
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT d.id, coalesce(d.round_until > now(), false) AS busy FROM documents d
                WHERE d.status = 'processing' AND {_OWNED} ORDER BY d.created_at""",
            (user.id,),
        ).fetchall()
    return [ProcessingDocument(**r) for r in rows]


@router.post("/documents/{document_id}/process", response_model=Document)
def process_round(document_id: str, user: User = Depends(current_user)):
    """One round of processing (PROCESSING_ROUND_SECONDS at most). Returns at once if a round is already running."""
    with db.connect() as conn:
        _get_doc_or_404(conn, document_id, user)
    ingestion.run_round(document_id)
    with db.connect() as conn:
        return _to_model(_get_doc_or_404(conn, document_id, user))


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        row = _get_doc_or_404(conn, document_id, user)
        conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))  # cascades to sections, chunks, figures
    storage = get_storage()
    storage.delete(row["stored_path"])
    storage.delete_prefix(f"{row['kb_id']}/figures/{document_id}")
    return Response(status_code=204)


# --------------------------------------------------------------------------- original files and diagrams

# Opened by <img> tags and plain links, which can't send an Authorization header, so these routes also accept
# a media token in the URL (see media_user), and main.py includes them without the usual sign-in check.
media_router = APIRouter(prefix="/api", tags=["documents"])


@media_router.get("/documents/{document_id}/file")
def get_document_file(document_id: str, user: User = Depends(media_user)):
    with db.connect() as conn:
        row = _get_doc_or_404(conn, document_id, user)
    media_type = mimetypes.guess_type(row["filename"])[0] or "application/octet-stream"
    try:
        return get_storage().response(row["stored_path"], row["filename"], media_type)
    except FileMissing:
        raise HTTPException(404, "The original file is missing from storage") from None


@media_router.get("/figures/{figure_id}")
def get_figure(figure_id: str, user: User = Depends(media_user)):
    """A diagram or picture cut out of a document (WebP)."""
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT f.stored_path FROM figures f JOIN documents d ON d.id = f.document_id WHERE f.id = %s AND {_OWNED}",
            (figure_id, user.id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Figure not found")
    try:
        response = get_storage().response(row["stored_path"], f"figure-{figure_id}.webp", "image/webp")
    except FileMissing:
        raise HTTPException(404, "The figure is missing from storage") from None
    response.headers["Cache-Control"] = "private, max-age=300"  # as long as an S3 download link lasts
    return response


# --------------------------------------------------------------------------- sections (the editable Markdown)


def _section_model(row: dict, file_type: str, figures: list[Figure]) -> Section:
    can_ocr = file_type == "image" or (file_type == "pdf" and row["page"] is not None)
    return Section(**{k: row[k] for k in Section.model_fields if k in row}, can_ocr=can_ocr, figures=figures)


def _get_section(conn: psycopg.Connection, section_id: str, user: User) -> tuple[dict, dict]:
    section = conn.execute("SELECT * FROM sections WHERE id = %s", (section_id,)).fetchone()
    document = section and conn.execute(
        f"SELECT * FROM documents d WHERE d.id = %s AND {_OWNED}", (section["document_id"], user.id)
    ).fetchone()
    if not document:
        raise HTTPException(404, "Section not found")
    return section, document


@router.get("/documents/{document_id}/sections", response_model=list[Section])
def list_sections(document_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        document = _get_doc_or_404(conn, document_id, user)
        rows = conn.execute("SELECT * FROM sections WHERE document_id = %s ORDER BY idx", (document_id,)).fetchall()
    figures = figures_by_section([r["id"] for r in rows])
    return [_section_model(r, document["file_type"], figures.get(r["id"], [])) for r in rows]


def _edit_section(section_id: str, user: User, action) -> Section:
    with db.connect() as conn:
        _, document = _get_section(conn, section_id, user)
    if document["status"] == "processing":
        raise HTTPException(409, "This document is still being processed. Try again when it's ready.")
    try:
        action()
    except ExtractionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, nvidia.describe_api_error(exc)) from exc
    with db.connect() as conn:
        section, document = _get_section(conn, section_id, user)
    return _section_model(section, document["file_type"], figures_by_section([section_id]).get(section_id, []))


@router.put("/sections/{section_id}", response_model=Section)
def update_section(section_id: str, body: SectionUpdate, user: User = Depends(current_user)):
    """Save edited Markdown for one section and re-index it."""
    return _edit_section(section_id, user, lambda: ingestion.update_section(section_id, body.markdown.strip()))


@router.post("/sections/{section_id}/ocr", response_model=Section)
def ocr_section(section_id: str, user: User = Depends(current_user)):
    """Run OCR again on this page (or image) and replace the section's Markdown."""
    limits.check_ocr(user.id)
    return _edit_section(section_id, user, lambda: ingestion.ocr_section(section_id))
