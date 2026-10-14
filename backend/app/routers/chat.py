import json
import logging
from datetime import datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb

from .. import db, limits, nvidia, rag
from ..auth import User, current_user
from ..config import get_settings
from ..schemas import (
    ChatRequest,
    Conversation,
    ConversationCreate,
    ConversationDetail,
    ConversationUpdate,
    Message,
    Source,
)

router = APIRouter(prefix="/api/conversations", tags=["chat"])
logger = logging.getLogger(__name__)

DEFAULT_TITLE = "New chat"


def _existing_kb_ids(conn: psycopg.Connection, kb_ids: list[str], owner_id: str) -> list[str]:
    """Keep only ids of the owner's knowledge bases that still exist, preserving order."""
    if not kb_ids:
        return []
    found = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM knowledge_bases WHERE id = ANY(%s) AND owner_id = %s", (kb_ids, owner_id)
        )
    }
    return [k for k in dict.fromkeys(kb_ids) if k in found]


def _conversation(conn: psycopg.Connection, row: dict) -> Conversation:
    return Conversation(**{**row, "kb_ids": _existing_kb_ids(conn, row["kb_ids"], row["owner_id"])})


def _message(row: dict) -> Message:
    data = dict(row)
    data.pop("conversation_id", None)
    return Message(**data)


def _get_row_or_404(conn: psycopg.Connection, conversation_id: str, user: User) -> dict:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = %s AND owner_id = %s", (conversation_id, user.id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Conversation not found")
    return row


@router.get("", response_model=list[Conversation])
def list_conversations(user: User = Depends(current_user)):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE owner_id = %s ORDER BY updated_at DESC", (user.id,)
        ).fetchall()
        return [_conversation(conn, r) for r in rows]


@router.post("", response_model=Conversation, status_code=201)
def create_conversation(body: ConversationCreate, user: User = Depends(current_user)):
    conversation_id = db.new_id()
    with db.connect() as conn:
        kb_ids = _existing_kb_ids(conn, body.kb_ids, user.id)
        conn.execute(
            "INSERT INTO conversations (id, owner_id, title, kb_ids) VALUES (%s, %s, %s, %s)",
            (conversation_id, user.id, DEFAULT_TITLE, Jsonb(kb_ids)),
        )
        return _conversation(conn, _get_row_or_404(conn, conversation_id, user))


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        conversation = _conversation(conn, _get_row_or_404(conn, conversation_id, user))
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = %s ORDER BY created_at", (conversation_id,)
        ).fetchall()
    return ConversationDetail(**conversation.model_dump(), messages=[_message(r) for r in rows])


@router.patch("/{conversation_id}", response_model=Conversation)
def update_conversation(conversation_id: str, body: ConversationUpdate, user: User = Depends(current_user)):
    with db.connect() as conn:
        _get_row_or_404(conn, conversation_id, user)
        if body.title is not None:
            conn.execute("UPDATE conversations SET title = %s WHERE id = %s", (body.title, conversation_id))
        if body.kb_ids is not None:
            conn.execute(
                "UPDATE conversations SET kb_ids = %s WHERE id = %s",
                (Jsonb(_existing_kb_ids(conn, body.kb_ids, user.id)), conversation_id),
            )
        return _conversation(conn, _get_row_or_404(conn, conversation_id, user))


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, user: User = Depends(current_user)):
    with db.connect() as conn:
        _get_row_or_404(conn, conversation_id, user)
        conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
    return Response(status_code=204)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/{conversation_id}/messages")
async def send_message(conversation_id: str, body: ChatRequest, user: User = Depends(current_user)):
    """Answer a question with retrieval-augmented generation, streamed as Server-Sent Events.

    Events: `sources` (retrieved passages), `token` (answer text), `done` (saved messages), `error`.
    """
    settings = get_settings()
    with db.connect() as conn:
        conversation = _get_row_or_404(conn, conversation_id, user)
        limits.check_chat(user.id)
        kb_ids = _existing_kb_ids(conn, body.kb_ids, user.id)
        conn.execute("UPDATE conversations SET kb_ids = %s WHERE id = %s", (Jsonb(kb_ids), conversation_id))
        history_rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at DESC LIMIT %s",
            (conversation_id, settings.max_history_messages),
        ).fetchall()
    history = [dict(r) for r in reversed(history_rows)]
    question = body.content
    asked_at = datetime.now(timezone.utc)

    def save(answer: str, sources: list[Source]) -> dict:
        user_msg = Message(id=db.new_id(), role="user", content=question, sources=[], created_at=asked_at)
        assistant_msg = Message(
            id=db.new_id(), role="assistant", content=answer, sources=sources, created_at=datetime.now(timezone.utc)
        )
        with db.connect() as conn:
            for m in (user_msg, assistant_msg):
                conn.execute(
                    """INSERT INTO messages (id, conversation_id, role, content, sources, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (m.id, conversation_id, m.role, m.content, Jsonb([s.model_dump() for s in m.sources]), m.created_at),
                )
            if conversation["title"] == DEFAULT_TITLE and not history:
                title = question if len(question) <= 60 else question[:57].rstrip() + "..."
                conn.execute("UPDATE conversations SET title = %s WHERE id = %s", (title, conversation_id))
            conn.execute("UPDATE conversations SET updated_at = %s WHERE id = %s", (assistant_msg.created_at, conversation_id))
        return {"user_message": user_msg.model_dump(mode="json"), "assistant_message": assistant_msg.model_dump(mode="json")}

    async def events():
        answer: list[str] = []
        sources: list[Source] = []
        saved = False
        try:
            sources = await rag.retrieve(question, history, kb_ids)
            yield _sse("sources", [s.model_dump() for s in sources])

            messages = rag.build_messages(question, history, sources, use_kb=bool(kb_ids))
            async for token in nvidia.stream_chat(messages):
                answer.append(token)
                yield _sse("token", {"text": token})

            saved = True
            yield _sse("done", save("".join(answer), sources))
        except Exception as exc:
            if not isinstance(exc, nvidia.NvidiaConfigError):
                logger.exception("Chat request failed")
            yield _sse("error", {"message": nvidia.describe_api_error(exc)})
        finally:
            # The user pressed Stop (client disconnected) or the stream broke mid-answer: keep the partial answer.
            if not saved and answer:
                save("".join(answer), sources)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
