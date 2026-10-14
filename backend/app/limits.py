"""Per-user rate limits and quotas, so a public deployment can't run up your NVIDIA bill or fill the database.

Rate limits are kept in memory: they reset when the server restarts and apply per server process.
That is right for a single instance; several instances behind a load balancer would need a shared store.
Every limit is set in .env, and 0 turns it off.
"""

import threading
import time
from collections import defaultdict, deque

import psycopg
from fastapi import HTTPException

from .config import get_settings

MINUTE = 60
HOUR = 3600
DAY = 86400


class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int, message: str, cost: int = 1) -> None:
        if limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) + cost > limit:
                retry_after = int(hits[0] + window - now) + 1 if hits else window
                raise HTTPException(429, message, headers={"Retry-After": str(retry_after)})
            hits.extend([now] * cost)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def check_chat(user_id: str) -> None:
    s = get_settings()
    limiter.hit(f"chat-minute:{user_id}", s.limit_chat_per_minute, MINUTE,
                "You're sending messages too quickly. Wait a minute and try again.")
    limiter.hit(f"chat-day:{user_id}", s.limit_chat_per_day, DAY,
                "You've reached today's message limit. Try again tomorrow.")


def check_ocr(user_id: str) -> None:
    limiter.hit(f"ocr-hour:{user_id}", get_settings().limit_ocr_per_hour, HOUR,
                "You've re-run OCR too often this hour. Try again later.")


def check_upload(conn: psycopg.Connection, user_id: str, file_count: int) -> None:
    """Checked before files are read; storage space is checked per file as it arrives."""
    s = get_settings()
    if s.max_files_per_upload and file_count > s.max_files_per_upload:
        raise HTTPException(400, f"Upload at most {s.max_files_per_upload} files at a time.")
    if s.limit_documents_per_user:
        existing = _owned_documents(conn, user_id)["count"]
        if existing + file_count > s.limit_documents_per_user:
            raise HTTPException(
                403,
                f"Your account can hold {s.limit_documents_per_user} files and has {existing}. "
                "Delete some files to upload more.",
            )
    limiter.hit(f"upload-day:{user_id}", s.limit_uploads_per_day, DAY,
                f"You can upload {s.limit_uploads_per_day} files a day. Try again tomorrow.", cost=file_count)


def storage_left_bytes(conn: psycopg.Connection, user_id: str) -> int | None:
    """Bytes this user may still upload, or None when storage is unlimited."""
    limit_mb = get_settings().limit_storage_mb_per_user
    if not limit_mb:
        return None
    return max(0, limit_mb * 1024 * 1024 - _owned_documents(conn, user_id)["bytes"])


def check_knowledge_base_quota(conn: psycopg.Connection, user_id: str) -> None:
    limit = get_settings().limit_kbs_per_user
    if not limit:
        return
    count = conn.execute("SELECT count(*) AS n FROM knowledge_bases WHERE owner_id = %s", (user_id,)).fetchone()["n"]
    if count >= limit:
        raise HTTPException(403, f"Your account can have {limit} knowledge bases. Delete one to create another.")


def _owned_documents(conn: psycopg.Connection, user_id: str) -> dict:
    return conn.execute(
        """SELECT count(*) AS count, COALESCE(sum(d.size_bytes), 0) AS bytes
           FROM documents d JOIN knowledge_bases kb ON kb.id = d.kb_id
           WHERE kb.owner_id = %s""",
        (user_id,),
    ).fetchone()
