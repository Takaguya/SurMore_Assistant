export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  created_at: string;
  document_count: number;
  ready_count: number;
}

export type DocumentStatus = "processing" | "ready" | "failed";

export interface DocumentInfo {
  id: string;
  kb_id: string;
  filename: string;
  file_type: string;
  size_bytes: number;
  status: DocumentStatus;
  progress: string | null;
  error: string | null;
  chunk_count: number;
  section_count: number;
  ocr_sections: number;
  edited_sections: number;
  embedding_model: string | null;
  needs_reindex: boolean;
  created_at: string;
}

export interface Section {
  id: string;
  document_id: string;
  idx: number;
  location: string | null;
  page: number | null;
  markdown: string;
  origin: "text" | "ocr";
  edited: boolean;
  notice: string | null;
  can_ocr: boolean;
  updated_at: string;
}

export interface RejectedFile {
  filename: string;
  reason: string;
}

export interface UploadResult {
  documents: DocumentInfo[];
  rejected: RejectedFile[];
}

export interface Source {
  n: number;
  chunk_id: string;
  document_id: string;
  section_id: string | null;
  kb_id: string;
  filename: string;
  file_type: string;
  location: string | null;
  content: string;
  score: number;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources: Source[];
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  kb_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export interface Health {
  api_key_configured: boolean;
  llm_model: string;
  embedding_model: string;
  ocr_model: string;
  ocr_mode: string;
  max_upload_mb: number;
}
