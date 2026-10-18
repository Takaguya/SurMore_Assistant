"""Retrieval and prompt construction for knowledge-base chat."""

import asyncio
import logging
import re

from pgvector import HalfVector

from ..core import db
from ..core.config import get_settings
from ..schemas import Figure, Source
from . import nvidia

logger = logging.getLogger(__name__)

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
    """The passages that best answer the question, with the figures of their sections.

    With a reranker: fetch plenty of candidates by meaning and by keyword, let the reranker score each one
    against the question, keep the best. Without one: the passages closest in meaning.
    """
    if not kb_ids:
        return []
    settings = get_settings()
    query = _search_query(question, history)
    query_vector = await nvidia.embed_query(query)
    if settings.nvidia_rerank_model:
        candidates = await asyncio.to_thread(
            _search, kb_ids, query_vector, query, settings.rerank_candidates, settings.rerank_candidates // 2
        )
        rows = await _rerank(query, candidates, settings.retrieval_top_k)
    else:
        rows = await asyncio.to_thread(_search, kb_ids, query_vector, query, settings.retrieval_top_k, 0)
    figures = await asyncio.to_thread(figures_by_section, [row["section_id"] for row in rows])
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
            figures=figures.get(row["section_id"], []),
        )
        for i, row in enumerate(rows, start=1)
    ]


async def _rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """Best candidates first, scored by the reranker. If reranking fails, keep the search order."""
    if len(candidates) <= 1:
        return candidates[:top_k]
    passages = [
        (f"{c['filename']} ({c['location']})" if c["location"] else c["filename"]) + "\n" + c["content"]
        for c in candidates
    ]
    try:
        scores = await asyncio.to_thread(nvidia.rerank, query, passages)
    except Exception as exc:
        logger.warning("Reranking failed, using search order instead: %s", exc)
        return candidates[:top_k]
    ranked = sorted(zip(scores, candidates), key=lambda pair: pair[0], reverse=True)
    return [{**candidate, "score": score} for score, candidate in ranked[:top_k]]


_SEARCH_FILTER = """
    c.kb_id = ANY(%(kb_ids)s)
    AND d.status = 'ready'
    AND c.embedding_model = %(model)s
    AND vector_dims(c.embedding) = %(dims)s
"""


def _search(kb_ids: list[str], query_vector, query_text: str, k_meaning: int, k_keyword: int) -> list[dict]:
    """Passages closest in meaning (exact cosine search with pgvector), then up to k_keyword more that share
    words with the question, all from the selected knowledge bases.

    Only chunks embedded with the current model (and dimension) are comparable with the query.
    """
    params = {
        "q": HalfVector(query_vector),
        "text": query_text,
        "kb_ids": kb_ids,
        "model": get_settings().nvidia_embedding_model,
        "dims": len(query_vector),
        "k_meaning": k_meaning,
        "k_keyword": k_keyword,
    }
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.section_id, c.content, c.location, c.document_id, c.kb_id, d.filename, d.file_type,
                   1 - (c.embedding <=> %(q)s) AS score
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {_SEARCH_FILTER}
            ORDER BY c.embedding <=> %(q)s
            LIMIT %(k_meaning)s
            """,
            params,
        ).fetchall()
        if k_keyword:
            # Catches exact terms (names, acronyms, error codes) that search by meaning can miss.
            # The question's words are OR-ed: a passage only needs some of them.
            keyword_rows = conn.execute(
                f"""
                WITH search AS (SELECT replace(plainto_tsquery('english', %(text)s)::text, '&', '|')::tsquery AS q)
                SELECT c.id, c.section_id, c.content, c.location, c.document_id, c.kb_id, d.filename, d.file_type,
                       ts_rank(to_tsvector('english', c.content), search.q) AS score
                FROM chunks c JOIN documents d ON d.id = c.document_id CROSS JOIN search
                WHERE {_SEARCH_FILTER}
                  AND to_tsvector('english', c.content) @@ search.q
                ORDER BY score DESC
                LIMIT %(k_keyword)s
                """,
                params,
            ).fetchall()
            seen = {row["id"] for row in rows}
            rows += [row for row in keyword_rows if row["id"] not in seen]
    return rows


def figures_by_section(section_ids: list[str]) -> dict[str, list[Figure]]:
    if not section_ids:
        return {}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, section_id, width, height FROM figures WHERE section_id = ANY(%s) ORDER BY section_id, idx",
            (section_ids,),
        ).fetchall()
    figures: dict[str, list[Figure]] = {}
    for row in rows:
        figures.setdefault(row["section_id"], []).append(Figure(id=row["id"], width=row["width"], height=row["height"]))
    return figures


def relink_sources(sources: list[Source]) -> None:
    """Point the sources of saved answers at their document's current sections and figures (in place).

    Reading a file again replaces its sections and figures with new ids, which would leave older answers
    with missing diagrams and dead "Show in document" links. A section is found by its saved id, or else
    by its location ("Page 12"), which stays the same when the same file is read again.
    """
    if not sources:
        return
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, document_id, location FROM sections
               WHERE document_id = ANY(%s) AND (id = ANY(%s) OR location = ANY(%s))
               ORDER BY idx""",
            (
                list({s.document_id for s in sources}),
                [s.section_id for s in sources if s.section_id],
                list({s.location for s in sources if s.location}),
            ),
        ).fetchall()
    current = {row["id"] for row in rows}
    by_location: dict[tuple[str, str], str] = {}
    for row in rows:
        by_location.setdefault((row["document_id"], row["location"]), row["id"])

    for source in sources:
        if source.section_id not in current:
            source.section_id = by_location.get((source.document_id, source.location))
    figures = figures_by_section([s.section_id for s in sources if s.section_id])
    for source in sources:
        source.figures = figures.get(source.section_id, []) if source.section_id else []


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
