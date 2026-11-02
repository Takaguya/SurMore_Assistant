"""Who is making the request.

AUTH_MODE=none      single-user mode for running on your own machine; everything belongs to "local".
AUTH_MODE=supabase  visitors sign in with Supabase Auth (email link, Google...); the browser sends the
                    Supabase access token, which is verified here before any data is touched.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request

from . import db
from .config import get_settings

logger = logging.getLogger(__name__)

LOCAL_USER_ID = "local"
ASYMMETRIC_ALGORITHMS = ["ES256", "RS256", "EdDSA"]
CACHE_SECONDS = 300
# <img> tags and file links can't send an Authorization header, and when the frontend is hosted apart from the
# API no cookie reaches it either. So their URLs carry a media token instead: it only opens the user's own
# diagrams and original files (see media_user) and runs out after an hour, so a URL that ends up in a log or
# the browser history is of little use.
MEDIA_TOKEN_SECONDS = 3600


@dataclass(frozen=True)
class User:
    id: str
    email: str | None = None


_cache: dict[str, tuple[float, User]] = {}
_cache_lock = threading.Lock()
_jwks_client: jwt.PyJWKClient | None = None


async def current_user(request: Request) -> User:
    """FastAPI dependency: the signed-in user, or 401."""
    settings = get_settings()
    if settings.auth_mode == "none":
        return User(LOCAL_USER_ID)
    token = _token_from(request)
    if not token:
        raise HTTPException(401, "Sign in to continue.")
    return await _verify(token)


def _token_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _supabase_url() -> str:
    return get_settings().supabase_url.rstrip("/")


async def _verify(token: str) -> User:
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    try:
        algorithm = jwt.get_unverified_header(token).get("alg")
        if algorithm == "HS256":
            # Older Supabase projects sign with a shared secret. Without it, ask Supabase directly.
            if get_settings().supabase_jwt_secret:
                user, expires = _decode(token, get_settings().supabase_jwt_secret, ["HS256"])
            else:
                user, expires = await _verify_with_supabase(token)
        else:
            signing_key = await asyncio.to_thread(_jwks().get_signing_key_from_jwt, token)
            user, expires = _decode(token, signing_key.key, ASYMMETRIC_ALGORITHMS)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(401, "Your session has expired. Sign in again.") from exc
    except (jwt.PyJWKClientConnectionError, urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Couldn't reach Supabase to check a sign-in: %s", exc)
        raise HTTPException(503, "Couldn't check your sign-in right now. Try again in a moment.") from exc
    except (jwt.PyJWTError, ValueError) as exc:
        logger.info("Rejected sign-in token: %s", exc)
        raise HTTPException(401, "Your sign-in isn't valid. Sign in again.") from exc

    with _cache_lock:
        if len(_cache) > 10_000:
            for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                del _cache[k]
        _cache[key] = (min(expires, now + CACHE_SECONDS), user)
    return user


def _decode(token: str, key, algorithms: list[str]) -> tuple[User, float]:
    claims = jwt.decode(
        token,
        key,
        algorithms=algorithms,
        audience="authenticated",
        issuer=f"{_supabase_url()}/auth/v1",
        options={"require": ["exp", "sub"]},
    )
    return User(claims["sub"], claims.get("email")), float(claims["exp"])


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(f"{_supabase_url()}/auth/v1/.well-known/jwks.json", lifespan=3600)
    return _jwks_client


async def _verify_with_supabase(token: str) -> tuple[User, float]:
    request = urllib.request.Request(
        f"{_supabase_url()}/auth/v1/user",
        headers={"apikey": get_settings().supabase_anon_key, "Authorization": f"Bearer {token}"},
    )

    def fetch() -> dict:
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise jwt.InvalidTokenError(f"Supabase rejected the token ({exc.code})") from exc

    data = await asyncio.to_thread(fetch)
    claims = jwt.decode(token, options={"verify_signature": False})
    return User(data["id"], data.get("email")), float(claims.get("exp", time.time() + 60))


# --------------------------------------------------------------------------- media tokens


def issue_media_token(user: User) -> str:
    payload = f"{user.id}.{int(time.time()) + MEDIA_TOKEN_SECONDS}"
    return f"{payload}.{_media_signature(payload)}"


async def media_user(request: Request) -> User:
    """FastAPI dependency for diagram and file downloads: the media token in ?token=, or a normal sign-in."""
    token = request.query_params.get("token")
    if token is None or get_settings().auth_mode == "none":
        return await current_user(request)
    return await asyncio.to_thread(_verify_media_token, token)


def _verify_media_token(token: str) -> User:
    payload, _, signature = token.rpartition(".")
    user_id, _, expires = payload.rpartition(".")
    if not (user_id and expires.isdecimal()) or not hmac.compare_digest(
        signature.encode(), _media_signature(payload).encode()
    ):
        raise HTTPException(401, "This link isn't valid. Reload the page and try again.")
    if int(expires) < time.time():
        raise HTTPException(401, "This link has expired. Reload the page and try again.")
    return User(user_id)


def _media_signature(payload: str) -> str:
    digest = hmac.new(_media_key(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@lru_cache
def _media_key() -> bytes:
    """Signs media tokens. It's kept in the database so every server process, or serverless instance, shares it."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO app_secrets (name, value) VALUES ('media_token_key', %s) ON CONFLICT (name) DO NOTHING",
            (secrets.token_hex(32),),
        )
        row = conn.execute("SELECT value FROM app_secrets WHERE name = 'media_token_key'").fetchone()
    return bytes.fromhex(row["value"])
