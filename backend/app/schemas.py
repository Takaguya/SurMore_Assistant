from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class KnowledgeBaseCreate(BaseModel):
    name: Name
    description: Description = ""


class KnowledgeBaseUpdate(BaseModel):
    name: Name | None = None
    description: Description | None = None


class KnowledgeBase(BaseModel):
    id: str
    name: str
    description: str
    created_at: datetime
    document_count: int
    ready_count: int


class Document(BaseModel):
    id: str
    kb_id: str
    filename: str
    file_type: str
    size_bytes: int
    status: Literal["processing", "ready", "failed"]
    progress: str | None
    error: str | None
    chunk_count: int
    section_count: int
    ocr_sections: int
    edited_sections: int
    embedding_model: str | None
    needs_reindex: bool
    created_at: datetime


class RejectedFile(BaseModel):
    filename: str
    reason: str


class UploadResult(BaseModel):
    documents: list[Document]
    rejected: list[RejectedFile]


class Section(BaseModel):
    id: str
    document_id: str
    idx: int
    location: str | None
    page: int | None
    markdown: str
    origin: Literal["text", "ocr"]
    edited: bool
    notice: str | None
    can_ocr: bool
    updated_at: datetime


class SectionUpdate(BaseModel):
    markdown: Annotated[str, StringConstraints(max_length=200_000)]


class Source(BaseModel):
    n: int
    chunk_id: str
    document_id: str
    section_id: str | None = None
    kb_id: str
    filename: str
    file_type: str
    location: str | None
    content: str
    score: float


class Message(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    sources: list[Source]
    created_at: datetime


class Conversation(BaseModel):
    id: str
    title: str
    kb_ids: list[str]
    created_at: datetime
    updated_at: datetime


class ConversationDetail(Conversation):
    messages: list[Message]


class ConversationCreate(BaseModel):
    kb_ids: list[str] = []


class ConversationUpdate(BaseModel):
    title: Name | None = None
    kb_ids: list[str] | None = None


class ChatRequest(BaseModel):
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    kb_ids: list[str] = Field(default_factory=list)


class Health(BaseModel):
    api_key_configured: bool
    llm_model: str
    embedding_model: str
    ocr_model: str
    ocr_mode: str
    max_upload_mb: int
