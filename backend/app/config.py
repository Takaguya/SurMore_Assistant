from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # a blank line like `NVIDIA_EMBEDDING_DIMENSIONS=` means "use the default"
        extra="ignore",
    )

    # PostgreSQL with the pgvector extension (local Docker, Supabase, Neon...)
    database_url: str = "postgresql://surmore:surmore@localhost:5433/surmore"

    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_llm_model: str = ""
    nvidia_embedding_model: str = "nvidia/llama-3.2-nv-embedqa-1b-v2"
    nvidia_embedding_dimensions: int | None = None
    nvidia_embedding_send_input_type: bool = True
    nvidia_embedding_batch_size: int = 32
    nvidia_ocr_model: str = "nvidia/nemotron-parse"

    # auto: OCR only pages/slides with no readable text; always: OCR every PDF page; off: never
    ocr_mode: Literal["auto", "always", "off"] = "auto"
    ocr_concurrency: int = 4
    ocr_min_text_chars: int = 40

    llm_temperature: float = 0.2
    llm_top_p: float = 0.7
    llm_max_tokens: int = 1024

    chunk_size: int = 1200
    chunk_overlap: int = 200
    retrieval_top_k: int = 6
    max_history_messages: int = 8

    max_upload_mb: int = 200
    cors_origins: str = "http://localhost:5180,http://127.0.0.1:5180"
    data_dir: Path = BACKEND_DIR / "data"

    # Public hosting. In production the server refuses to start without sign-in.
    environment: Literal["development", "production"] = "development"
    auth_mode: Literal["none", "supabase"] = "none"
    supabase_url: str = ""
    supabase_anon_key: str = ""  # the public "anon"/publishable key; it is sent to the browser
    supabase_jwt_secret: str = ""  # only for projects still on Supabase's legacy shared JWT secret
    auth_providers: str = "email,google"

    # Original files: local disk, or an S3-compatible bucket (Cloudflare R2, Supabase Storage, AWS S3)
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = "auto"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Per-user limits; 0 means unlimited
    limit_chat_per_minute: int = 10
    limit_chat_per_day: int = 300
    limit_ocr_per_hour: int = 30
    limit_uploads_per_day: int = 50
    limit_kbs_per_user: int = 20
    limit_documents_per_user: int = 200
    limit_storage_mb_per_user: int = 1000
    max_files_per_upload: int = 20

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth_provider_list(self) -> list[str]:
        return [p.strip() for p in self.auth_providers.split(",") if p.strip()]

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()
