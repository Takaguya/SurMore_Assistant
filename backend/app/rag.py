"""Retrieval and prompt construction for knowledge-base chat."""

import asyncio
import re

from pgvector import HalfVector

from . import db, nvidia
from .config import get_settings
from .schemas import Source

SYSTEM_PROMPT = """You are SurMore Assistant. You answer questions using passages retrieved from the user's knowledge bases.

Rules:
- Base your answer on the context passages below. Cite the passages you use with their number in square brackets, like [1] or [2][3], right after the statement they support.
- If the passages don't contain the answer, say that the selected knowledge bases don't cover it. Don't invent facts, figures or quotes.
- Answer in the same language as the user's question. Use Markdown (lists, tables, bold) when it makes the answer clearer.

Context passages:
{context}"""

SYSTEM_PROMPT_NO_KB = (
    "You are SurMore Assistant, a helpful assistant. No knowledge base is selected for this chat, "
    "so answer from general knowledge. If the question seems to be about the user's own documents, "
    "suggest selecting a knowledge base."
)

NO_PASSAGES = "(No relevant passages were found in the selected knowledge bases.)"

# Follow-ups like "what about the second one?" carry little meaning on their own,
# so short questions are searched together with the previous question.
SHORT_QUESTION_WORDS = 8

# Reasoning models wrap their chain of thought in <think> tags; it isn't worth resending as history.
THINK_BLOCK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)


def _search_query(question: str, history: list[dict]) -> str:
    if len(question.split()) >= SHORT_QUESTION_WORDS:
        return question
    previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
    return f"{previous}\n{question}" if previous else question


async def retrieve(question: str, history: list[dict], kb_ids: list[str]) -> list[Source]:
    if not kb_ids:
        return []
    query_vector = await nvidia.embed_query(_search_query(question, history))
    rows = await asyncio.to_thread(_search, kb_ids, query_vector)
    return [
        Source(
            n=i,
            chunk_id=row["id"],
            document_id=row["document_id"],
            section_id=row["section_id"],
            kb_id=row["kb_id"],
            filename=row["filename"],
            file_type=row["file_type"],
            location=row["location"],
            content=row["content"],
            score=round(float(row["score"]), 4),
        )
        for i, row in enumerate(rows, start=1)
    ]


def _search(kb_ids: list[str], query_vector) -> list[dict]:
    """Exact cosine-similarity search with pgvector, limited to the selected knowledge bases.

    Only chunks embedded with the current model (and dimension) are comparable with the query.
    """
    settings = get_settings()
    query = HalfVector(query_vector)
    with db.connect() as conn:
        return conn.execute(
            """
            SELECT c.id, c.section_id, c.content, c.location, c.document_id, c.kb_id, d.filename, d.file_type,
                   1 - (c.embedding <=> %(q)s) AS score
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.kb_id = ANY(%(kb_ids)s)
              AND d.status = 'ready'
              AND c.embedding_model = %(model)s
              AND vector_dims(c.embedding) = %(dims)s
            ORDER BY c.embedding <=> %(q)s
            LIMIT %(k)s
            """,
            {
                "q": query,
                "kb_ids": kb_ids,
                "model": settings.nvidia_embedding_model,
                "dims": len(query_vector),
                "k": settings.retrieval_top_k,
            },
        ).fetchall()


def build_messages(question: str, history: list[dict], sources: list[Source], use_kb: bool) -> list[dict]:
    if use_kb:
        context = "\n\n".join(
            f"[{s.n}] {s.filename}{f' — {s.location}' if s.location else ''}\n{s.content}" for s in sources
        )
        system = SYSTEM_PROMPT.format(context=context or NO_PASSAGES)
    else:
        system = SYSTEM_PROMPT_NO_KB

    return [
        {"role": "system", "content": system},
        *({"role": m["role"], "content": THINK_BLOCK.sub("", m["content"]).strip()} for m in history),
        {"role": "user", "content": question},
    ]
