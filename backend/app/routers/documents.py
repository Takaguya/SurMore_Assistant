import mimetypes
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile

from .. import db, ingestion, limits, nvidia
from ..auth import User, current_user
from ..config import get_settings
from ..converters import ExtractionError, detect_file_type
from ..storage import FileMissing, FileTooLarge, get_storage
from ..schemas import Document, RejectedFile, Section, SectionUpdate, UploadResult
from .knowledge_bases import get_kb_or_404

router = APIRouter(prefix="/api", tags=["documents"])

# Only documents in knowledge bases the user owns are visible.
_OWNED = "d.kb_id IN (SELECT id FROM knowledge_bases WHERE owner_id = %s)"

_SELECT_DOCUMENT = """
SELECT d.*,
       (SELECT count(*) FROM sections s WHERE s.document_id = d.id) AS section_count,
       (SELECT count(*) FROM sections s WHERE s.document_id = d.id AND s.origin = 'ocr') AS ocr_sections,
       (SELECT count(*) FROM sections s WHERE s.document_id = d.id AND s.edited) AS edited_sections
FROM documents d
"""


def _to_model(row: dict) -> Document:
    data = dict(row)
    data.pop("stored_path")
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
        # Stored under a generated name; the original filename only lives in the database.
        stored_path = f"{kb_id}/{doc_id}{Path(filename).suffix.lower()}"
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
                """INSERT INTO documents (id, kb_id, filename, file_type, size_bytes, stored_path, status, progress)
                   VALUES (%s, %s, %s, %s, %s, %s, 'processing', 'Waiting to start')""",
                (doc_id, kb_id, filename, file_type, size, stored_path),
            )
        background.add_task(ingestion.process_document, doc_id)
        accepted.append(doc_id)

    with db.connect() as conn:
        rows = [_get_doc_or_404(conn, doc_id, user) for doc_id in accepted]
    return UploadResult(documents=[_to_model(r) for r in rows], rejected=rejected)


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
        if row["status"] == "processing":
            raise HTTPException(409, "This document is already being processed")
        if mode == "auto":
            mode = "reindex" if row["section_count"] else "extract"
        if mode == "reindex" and not row["section_count"]:
            raise HTTPException(400, "This document has no text to index yet. Read the file again instead.")
        conn.execute(
            "UPDATE documents SET status = 'processing', progress = 'Waiting to start', error = NULL WHERE id = %s",
            (document_id,),
        )
        row = _get_doc_or_404(conn, document_id, user)
    background.add_task(ingestion.process_document, document_id, mode)
    return _to_model(row)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        row = _get_doc_or_404(conn, document_id, user)
        conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))  # cascades to sections and chunks
    get_storage().delete(row["stored_path"])
    return Response(status_code=204)


@router.get("/documents/{document_id}/file")
def get_document_file(document_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        row = _get_doc_or_404(conn, document_id, user)
    media_type = mimetypes.guess_type(row["filename"])[0] or "application/octet-stream"
    try:
        return get_storage().response(row["stored_path"], row["filename"], media_type)
    except FileMissing:
        raise HTTPException(404, "The original file is missing from storage") from None


# --------------------------------------------------------------------------- sections (the editable Markdown)


def _section_model(row: dict, file_type: str) -> Section:
    can_ocr = file_type == "image" or (file_type == "pdf" and row["page"] is not None)
    return Section(**{k: row[k] for k in Section.model_fields if k in row}, can_ocr=can_ocr)


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
    return [_section_model(r, document["file_type"]) for r in rows]


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
    return _section_model(section, document["file_type"])


@router.put("/sections/{section_id}", response_model=Section)
def update_section(section_id: str, body: SectionUpdate, user: User = Depends(current_user)):
    """Save edited Markdown for one section and re-index it."""
    return _edit_section(section_id, user, lambda: ingestion.update_section(section_id, body.markdown.strip()))


@router.post("/sections/{section_id}/ocr", response_model=Section)
def ocr_section(section_id: str, user: User = Depends(current_user)):
    """Run OCR again on this page (or image) and replace the section's Markdown."""
    limits.check_ocr(user.id)
    return _edit_section(section_id, user, lambda: ingestion.ocr_section(section_id))
