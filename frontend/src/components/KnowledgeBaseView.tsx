import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { api } from "../api";
import { ACCEPTED_EXTENSIONS, countSections, fileTypeLabel, formatBytes } from "../format";
import type { DocumentInfo, KnowledgeBase, RejectedFile } from "../types";
import { ChatIcon, OpenIcon, RetryIcon, TrashIcon, UploadIcon } from "./Icons";
import { KbFormDialog } from "./KbFormDialog";

interface Props {
  kb: KnowledgeBase;
  maxUploadMb?: number;
  onChanged: () => void;
  onDeleted: () => void;
  onStartChat: (kbId: string) => void;
  onOpenDocument: (documentId: string) => void;
}

const POLL_MS = 2000;

export function KnowledgeBaseView({ kb, maxUploadMb, onChanged, onDeleted, onStartChat, onOpenDocument }: Props) {
  const [documents, setDocuments] = useState<DocumentInfo[] | null>(null);
  const [uploading, setUploading] = useState<number>(0);
  const [rejected, setRejected] = useState<RejectedFile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [editing, setEditing] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setDocuments(await api.listDocuments(kb.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [kb.id]);

  useEffect(() => {
    setDocuments(null);
    setRejected([]);
    setError(null);
    load();
  }, [load]);

  // Keep refreshing while files are being read and indexed.
  const processing = documents?.some((d) => d.status === "processing") ?? false;
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(async () => {
      await load();
    }, POLL_MS);
    return () => {
      clearInterval(timer);
      onChanged();
    };
  }, [processing, load, onChanged]);

  async function upload(files: File[]) {
    if (!files.length) return;
    setUploading(files.length);
    setRejected([]);
    setError(null);
    try {
      const result = await api.uploadDocuments(kb.id, files);
      setRejected(result.rejected);
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(0);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    upload(Array.from(e.dataTransfer.files));
  }

  async function retry(doc: DocumentInfo) {
    try {
      await api.reprocessDocument(doc.id, doc.needs_reindex ? "reindex" : "auto");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function remove(doc: DocumentInfo) {
    if (!confirm(`Remove “${doc.filename}” from ${kb.name}? The chatbot will stop using it.`)) return;
    try {
      await api.deleteDocument(doc.id);
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function deleteKb() {
    const count = kb.document_count === 1 ? "its 1 file" : `all ${kb.document_count} files`;
    if (!confirm(`Delete “${kb.name}” and ${count}? This can't be undone.`)) return;
    try {
      await api.deleteKnowledgeBase(kb.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="page">
      <div className="page-column">
        <header className="kb-header">
          <div>
            <h1>{kb.name}</h1>
            {kb.description && <p className="kb-description">{kb.description}</p>}
          </div>
          <div className="kb-actions">
            <button className="btn btn-primary" onClick={() => onStartChat(kb.id)} disabled={kb.ready_count === 0}>
              <ChatIcon /> Chat with this knowledge base
            </button>
            <button className="btn btn-quiet" onClick={() => setEditing(true)}>
              Rename
            </button>
            <button className="btn btn-quiet btn-danger-text" onClick={deleteKb}>
              Delete
            </button>
          </div>
        </header>

        <label
          className={`dropzone${dragging ? " dragging" : ""}${uploading ? " busy" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
        >
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={ACCEPTED_EXTENSIONS.join(",")}
            onChange={(e) => upload(Array.from(e.target.files ?? []))}
            disabled={uploading > 0}
            className="visually-hidden"
          />
          <UploadIcon width={26} height={26} />
          <span className="dropzone-title">
            {uploading
              ? `Uploading ${uploading === 1 ? "1 file" : `${uploading} files`}…`
              : "Drop files here or choose files"}
          </span>
          <span className="dropzone-hint">
            PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), CSV, text, Markdown and images{maxUploadMb ? `, up to ${maxUploadMb} MB each` : ""}.
          </span>
        </label>

        {error && (
          <p className="notice notice-error" role="alert">
            {error}
          </p>
        )}
        {rejected.length > 0 && (
          <div className="notice notice-error" role="alert">
            <p>{rejected.length === 1 ? "1 file wasn't added:" : `${rejected.length} files weren't added:`}</p>
            <ul>
              {rejected.map((r) => (
                <li key={r.filename}>
                  <strong>{r.filename}</strong>: {r.reason}
                </li>
              ))}
            </ul>
          </div>
        )}

        <section aria-labelledby="files-heading">
          <h2 id="files-heading" className="section-title">
            Files
            {documents && documents.length > 0 && <span className="count">{documents.length}</span>}
          </h2>
          {documents === null ? (
            <p className="muted">Loading files…</p>
          ) : documents.length === 0 ? (
            <p className="muted">No files yet. Upload lesson slides, notes or reports above and they'll be searchable in chat.</p>
          ) : (
            <ul className="doc-list">
              {documents.map((doc) => (
                <DocumentRow key={doc.id} doc={doc} onOpen={onOpenDocument} onRetry={retry} onRemove={remove} />
              ))}
            </ul>
          )}
        </section>
      </div>

      <KbFormDialog
        open={editing}
        title="Rename knowledge base"
        submitLabel="Save changes"
        initialName={kb.name}
        initialDescription={kb.description}
        onClose={() => setEditing(false)}
        onSubmit={async (name, description) => {
          await api.updateKnowledgeBase(kb.id, name, description);
          setEditing(false);
          onChanged();
        }}
      />
    </div>
  );
}

function DocumentRow({
  doc,
  onOpen,
  onRetry,
  onRemove,
}: {
  doc: DocumentInfo;
  onOpen: (documentId: string) => void;
  onRetry: (d: DocumentInfo) => void;
  onRemove: (d: DocumentInfo) => void;
}) {
  const status =
    doc.status === "processing"
      ? (doc.progress ?? "Reading…")
      : doc.status === "failed"
        ? "Failed"
        : doc.needs_reindex
          ? "Needs re-indexing"
          : countSections(doc.file_type, doc.section_count);

  return (
    <li className={`doc-row status-${doc.status}${doc.needs_reindex ? " status-stale" : ""}`}>
      <span className={`type-tab type-${doc.file_type}`}>{fileTypeLabel(doc.file_type)}</span>
      <div className="doc-main">
        <button className="doc-name" onClick={() => onOpen(doc.id)} title="View and edit the text">
          {doc.filename}
        </button>
        <span className="doc-meta">
          <span>{formatBytes(doc.size_bytes)}</span>
          <span className="doc-status" aria-live="polite">
            {doc.status === "processing" && <span className="spinner" aria-hidden="true" />}
            {status}
          </span>
          {doc.ocr_sections > 0 && <span>{doc.ocr_sections} read with OCR</span>}
          {doc.edited_sections > 0 && <span>{doc.edited_sections} edited</span>}
        </span>
        {doc.error && <p className="doc-error">{doc.error}</p>}
        {doc.needs_reindex && (
          <p className="doc-error">Indexed with a different embedding model ({doc.embedding_model}). Re-index to search it.</p>
        )}
      </div>
      <div className="doc-actions">
        {(doc.status === "failed" || doc.needs_reindex) && (
          <button className="btn btn-small" onClick={() => onRetry(doc)}>
            <RetryIcon width={15} height={15} /> {doc.needs_reindex ? "Re-index" : "Retry"}
          </button>
        )}
        <a
          className="icon-btn"
          href={api.documentFileUrl(doc.id)}
          target="_blank"
          rel="noreferrer"
          aria-label={`Open the original ${doc.filename}`}
          title="Open original file"
        >
          <OpenIcon width={16} height={16} />
        </a>
        <button
          className="icon-btn"
          onClick={() => onRemove(doc)}
          disabled={doc.status === "processing"}
          aria-label={`Remove ${doc.filename}`}
          title="Remove file"
        >
          <TrashIcon width={16} height={16} />
        </button>
      </div>
    </li>
  );
}
