"""Thin wrappers around NVIDIA's OpenAI-compatible API (embeddings + chat)."""

from collections.abc import AsyncIterator, Callable
from functools import lru_cache

import numpy as np
import openai
from openai import AsyncOpenAI, OpenAI

from .config import get_settings


class NvidiaConfigError(RuntimeError):
    """Raised when a required NVIDIA setting is missing from .env."""


def check_key() -> None:
    if not get_settings().nvidia_api_key:
        raise NvidiaConfigError("NVIDIA_API_KEY is not set. Add it to backend/.env and restart the server.")


@lru_cache
def sync_client() -> OpenAI:
    s = get_settings()
    return OpenAI(api_key=s.nvidia_api_key, base_url=s.nvidia_base_url, timeout=120, max_retries=2)


@lru_cache
def _async_client() -> AsyncOpenAI:
    s = get_settings()
    return AsyncOpenAI(api_key=s.nvidia_api_key, base_url=s.nvidia_base_url, timeout=120, max_retries=2)


def _embedding_kwargs(input_type: str) -> dict:
    s = get_settings()
    kwargs: dict = {"model": s.nvidia_embedding_model, "encoding_format": "float"}
    if s.nvidia_embedding_dimensions:
        kwargs["dimensions"] = s.nvidia_embedding_dimensions
    if s.nvidia_embedding_send_input_type:
        kwargs["extra_body"] = {"input_type": input_type, "truncate": "END"}
    return kwargs


def describe_api_error(exc: Exception) -> str:
    """Turn an API exception into a message that tells the user what to fix."""
    if isinstance(exc, NvidiaConfigError):
        return str(exc)
    if isinstance(exc, openai.AuthenticationError):
        return "NVIDIA rejected the API key. Check NVIDIA_API_KEY in backend/.env."
    if isinstance(exc, openai.NotFoundError):
        return f"NVIDIA couldn't find the model. Check the model names in backend/.env. ({exc.message})"
    if isinstance(exc, openai.RateLimitError):
        return "NVIDIA rate limit reached. Wait a moment and try again."
    if isinstance(exc, openai.APIConnectionError):
        return f"Couldn't reach NVIDIA at {get_settings().nvidia_base_url}. Check your connection and NVIDIA_BASE_URL."
    if isinstance(exc, openai.APIStatusError):
        return f"NVIDIA API error {exc.status_code}: {exc.message}"
    return str(exc) or exc.__class__.__name__


def embed_passages(texts: list[str], on_progress: Callable[[int, int], None] | None = None) -> np.ndarray:
    """Embed document chunks (blocking). Returns an (n, dim) float32 array."""
    check_key()
    s = get_settings()
    client = sync_client()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), s.nvidia_embedding_batch_size):
        batch = texts[start : start + s.nvidia_embedding_batch_size]
        resp = client.embeddings.create(input=batch, **_embedding_kwargs("passage"))
        vectors.extend(item.embedding for item in sorted(resp.data, key=lambda d: d.index))
        if on_progress:
            on_progress(len(vectors), len(texts))
    return np.asarray(vectors, dtype=np.float32)


async def embed_query(text: str) -> np.ndarray:
    check_key()
    resp = await _async_client().embeddings.create(input=[text], **_embedding_kwargs("query"))
    return np.asarray(resp.data[0].embedding, dtype=np.float32)


async def stream_chat(messages: list[dict]) -> AsyncIterator[str]:
    check_key()
    s = get_settings()
    if not s.nvidia_llm_model:
        raise NvidiaConfigError("NVIDIA_LLM_MODEL is not set. Add it to backend/.env and restart the server.")
    stream = await _async_client().chat.completions.create(
        model=s.nvidia_llm_model,
        messages=messages,
        temperature=s.llm_temperature,
        top_p=s.llm_top_p,
        max_tokens=s.llm_max_tokens,
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
