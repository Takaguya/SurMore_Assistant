import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from .. import db, limits
from ..auth import User, current_user
from ..storage import get_storage
from ..schemas import KnowledgeBase, KnowledgeBaseCreate, KnowledgeBaseUpdate

router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge bases"])

_SELECT = """
SELECT kb.*,
       count(d.id) AS document_count,
       count(d.id) FILTER (WHERE d.status = 'ready') AS ready_count
FROM knowledge_bases kb
LEFT JOIN documents d ON d.kb_id = kb.id
"""


def get_kb_or_404(conn: psycopg.Connection, kb_id: str, user: User) -> KnowledgeBase:
    """The knowledge base, if it exists and belongs to this user; another user's id looks like a missing one."""
    row = conn.execute(_SELECT + " WHERE kb.id = %s AND kb.owner_id = %s GROUP BY kb.id", (kb_id, user.id)).fetchone()
    if row is None:
        raise HTTPException(404, "Knowledge base not found")
    return KnowledgeBase(**row)


@router.get("", response_model=list[KnowledgeBase])
def list_knowledge_bases(user: User = Depends(current_user)):
    with db.connect() as conn:
        rows = conn.execute(
            _SELECT + " WHERE kb.owner_id = %s GROUP BY kb.id ORDER BY kb.created_at DESC", (user.id,)
        ).fetchall()
    return [KnowledgeBase(**r) for r in rows]


@router.post("", response_model=KnowledgeBase, status_code=201)
def create_knowledge_base(body: KnowledgeBaseCreate, user: User = Depends(current_user)):
    kb_id = db.new_id()
    with db.connect() as conn:
        limits.check_knowledge_base_quota(conn, user.id)
        conn.execute(
            "INSERT INTO knowledge_bases (id, owner_id, name, description) VALUES (%s, %s, %s, %s)",
            (kb_id, user.id, body.name, body.description),
        )
        return get_kb_or_404(conn, kb_id, user)


@router.get("/{kb_id}", response_model=KnowledgeBase)
def get_knowledge_base(kb_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        return get_kb_or_404(conn, kb_id, user)


@router.patch("/{kb_id}", response_model=KnowledgeBase)
def update_knowledge_base(kb_id: str, body: KnowledgeBaseUpdate, user: User = Depends(current_user)):
    with db.connect() as conn:
        get_kb_or_404(conn, kb_id, user)
        conn.execute(
            """UPDATE knowledge_bases SET name = COALESCE(%s, name), description = COALESCE(%s, description)
               WHERE id = %s""",
            (body.name, body.description, kb_id),
        )
        return get_kb_or_404(conn, kb_id, user)


@router.delete("/{kb_id}", status_code=204)
def delete_knowledge_base(kb_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        get_kb_or_404(conn, kb_id, user)
        conn.execute("DELETE FROM knowledge_bases WHERE id = %s", (kb_id,))  # cascades to documents, sections, chunks
    get_storage().delete_prefix(kb_id)
    return Response(status_code=204)
