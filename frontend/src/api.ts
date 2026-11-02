import type {
  Conversation,
  ConversationDetail,
  DocumentInfo,
  Health,
  KnowledgeBase,
  Message,
  Section,
  Source,
  UploadResult,
} from "./types";
import { API_BASE as BASE, getAccessToken, UNAUTHORIZED_EVENT } from "./auth";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function errorFrom(res: Response): Promise<ApiError> {
  if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  let message = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (typeof body.detail === "string") message = body.detail;
    else if (Array.isArray(body.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join("; ");
  } catch {
    // body wasn't JSON; keep the generic message
  }
  return new ApiError(message, res.status);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const auth = await authHeaders();
  const res = await fetch(BASE + path, {
    ...init,
    headers:
      init?.body instanceof FormData
        ? { ...auth, ...init.headers }
        : { "Content-Type": "application/json", ...auth, ...init?.headers },
  });
  if (!res.ok) throw await errorFrom(res);
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });

/** Signs a diagram or file URL in with the media token (see useMediaToken); nothing when sign-in is off. */
const withMediaToken = (mediaToken: string | null) => (mediaToken ? `?token=${encodeURIComponent(mediaToken)}` : "");

export const api = {
  health: () => request<Health>("/api/health"),

  listKnowledgeBases: () => request<KnowledgeBase[]>("/api/knowledge-bases"),
  createKnowledgeBase: (name: string, description: string) =>
    request<KnowledgeBase>("/api/knowledge-bases", json("POST", { name, description })),
  updateKnowledgeBase: (id: string, name: string, description: string) =>
    request<KnowledgeBase>(`/api/knowledge-bases/${id}`, json("PATCH", { name, description })),
  deleteKnowledgeBase: (id: string) => request<void>(`/api/knowledge-bases/${id}`, { method: "DELETE" }),

  listDocuments: (kbId: string) => request<DocumentInfo[]>(`/api/knowledge-bases/${kbId}/documents`),
  uploadDocuments: (kbId: string, files: File[]) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    return request<UploadResult>(`/api/knowledge-bases/${kbId}/documents`, { method: "POST", body: form });
  },
  /** Sends the file straight to the bucket with a one-time link, past the API host's request size limit. */
  uploadDocumentDirect: async (kbId: string, file: File) => {
    const link = await request<{ url: string; document_id: string }>(
      `/api/knowledge-bases/${kbId}/uploads`,
      json("POST", { filename: file.name, size: file.size }),
    );
    // No Authorization header: the link itself is the permission.
    const res = await fetch(link.url, { method: "PUT", body: file });
    if (!res.ok) throw new ApiError(`Couldn't upload ${file.name} (${res.status}). Try again.`, res.status);
    return request<DocumentInfo>(
      `/api/knowledge-bases/${kbId}/uploads/${link.document_id}`,
      json("POST", { filename: file.name }),
    );
  },
  getDocument: (id: string) => request<DocumentInfo>(`/api/documents/${id}`),
  /** auto: re-index if the document has text, else read the file. extract: read the file again (replaces edits). */
  reprocessDocument: (id: string, mode: "auto" | "extract" | "reindex" = "auto") =>
    request<DocumentInfo>(`/api/documents/${id}/reprocess?mode=${mode}`, { method: "POST" }),
  deleteDocument: (id: string) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),
  /** Documents still being processed, and whether a round is working on each right now. */
  listProcessing: () => request<{ id: string; busy: boolean }[]>("/api/processing"),
  /** One round of processing; takes up to a few minutes. */
  processDocument: (id: string) => request<DocumentInfo>(`/api/documents/${id}/process`, { method: "POST" }),
  documentFileUrl: (id: string, mediaToken: string | null, fileType?: string, location?: string | null) => {
    const page = fileType === "pdf" ? location?.match(/^Page (\d+)/)?.[1] : undefined;
    return `${BASE}/api/documents/${id}/file${withMediaToken(mediaToken)}${page ? `#page=${page}` : ""}`;
  },

  /** Plain URL for <img src>, which can't send headers, so sign-in rides along as the media token. */
  figureUrl: (id: string, mediaToken: string | null) => `${BASE}/api/figures/${id}${withMediaToken(mediaToken)}`,

  listSections: (documentId: string) => request<Section[]>(`/api/documents/${documentId}/sections`),
  updateSection: (id: string, markdown: string) => request<Section>(`/api/sections/${id}`, json("PUT", { markdown })),
  ocrSection: (id: string) => request<Section>(`/api/sections/${id}/ocr`, { method: "POST" }),

  listConversations: () => request<Conversation[]>("/api/conversations"),
  createConversation: (kbIds: string[]) =>
    request<Conversation>("/api/conversations", json("POST", { kb_ids: kbIds })),
  getConversation: (id: string) => request<ConversationDetail>(`/api/conversations/${id}`),
  updateConversation: (id: string, patch: { title?: string; kb_ids?: string[] }) =>
    request<Conversation>(`/api/conversations/${id}`, json("PATCH", patch)),
  deleteConversation: (id: string) => request<void>(`/api/conversations/${id}`, { method: "DELETE" }),

  streamMessage,
};

export interface StreamHandlers {
  onSources: (sources: Source[]) => void;
  onToken: (text: string) => void;
  onDone: (saved: { user_message: Message; assistant_message: Message }) => void;
  onError: (message: string) => void;
}

/** POST a question and read the Server-Sent Events answer stream. */
async function streamMessage(
  conversationId: string,
  content: string,
  kbIds: string[],
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/api/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ content, kb_ids: kbIds }),
    signal,
  });
  if (!res.ok || !res.body) throw await errorFrom(res);

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, "\n");
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      dispatch(buffer.slice(0, boundary), handlers);
      buffer = buffer.slice(boundary + 2);
    }
  }
}

function dispatch(raw: string, handlers: StreamHandlers) {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return;
  const payload = JSON.parse(data.join("\n"));
  if (event === "sources") handlers.onSources(payload);
  else if (event === "token") handlers.onToken(payload.text);
  else if (event === "done") handlers.onDone(payload);
  else if (event === "error") handlers.onError(payload.message);
}
