import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import { countSections, fileTypeLabel } from "../format";
import type { DocumentInfo, KnowledgeBase, Section } from "../types";
import { ChevronLeftIcon, OpenIcon, RetryIcon } from "./Icons";

interface Props {
  documentId: string;
  kb: KnowledgeBase | undefined;
  targetSectionId: string | null;
  onBack: () => void;
  onChanged: () => void;
}

const BATCH = 20;
const POLL_MS = 2000;

export function DocumentView({ documentId, kb, targetSectionId, onBack, onChanged }: Props) {
  const [doc, setDoc] = useState<DocumentInfo | null>(null);
  const [sections, setSections] = useState<Section[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(BATCH);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [ocrId, setOcrId] = useState<string | null>(null);
  const [sectionError, setSectionError] = useState<{ id: string; message: string } | null>(null);
  const pendingScroll = useRef<string | null>(targetSectionId);

  const report = (err: unknown) => setError(err instanceof Error ? err.message : String(err));

  const loadDoc = useCallback(async () => {
    const d = await api.getDocument(documentId);
    setDoc(d);
    return d;
  }, [documentId]);

  const loadSections = useCallback(async () => {
    setSections(await api.listSections(documentId));
  }, [documentId]);

  useEffect(() => {
    loadDoc()
      .then((d) => (d.status !== "processing" ? loadSections() : undefined))
      .catch(report);
  }, [loadDoc, loadSections]);

  // While the file is being read (OCR can take minutes), show progress and load the text when it's done.
  const processing = doc?.status === "processing";
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(async () => {
      try {
        const d = await loadDoc();
        if (d.status !== "processing") {
          await loadSections();
          onChanged();
        }
      } catch (err) {
        report(err);
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [processing, loadDoc, loadSections, onChanged]);

  // Coming from a chat citation: render far enough down, then scroll to the cited section.
  useEffect(() => {
    if (!sections || !pendingScroll.current) return;
    const index = sections.findIndex((s) => s.id === pendingScroll.current);
    if (index === -1) pendingScroll.current = null;
    else if (index >= visible) setVisible(index + BATCH / 2);
  }, [sections, visible]);

  useLayoutEffect(() => {
    const id = pendingScroll.current;
    const el = id ? document.getElementById(`section-${id}`) : null;
    if (el) {
      el.scrollIntoView({ block: "start" });
      pendingScroll.current = null;
    }
  }, [sections, visible]);

  function replaceSection(updated: Section) {
    setSections((list) => list?.map((s) => (s.id === updated.id ? updated : s)) ?? null);
    loadDoc().catch(report);
  }

  async function save(section: Section, markdown: string) {
    const updated = await api.updateSection(section.id, markdown);
    replaceSection(updated);
    setEditingId(null);
  }

  async function runOcr(section: Section) {
    if (section.edited && !confirm("Run OCR on this page again? Your edits to it will be replaced.")) return;
    setOcrId(section.id);
    setSectionError(null);
    try {
      replaceSection(await api.ocrSection(section.id));
    } catch (err) {
      setSectionError({ id: section.id, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setOcrId(null);
    }
  }

  async function reprocess(mode: "auto" | "extract" | "reindex") {
    if (mode === "extract" && doc && doc.edited_sections > 0) {
      const n = doc.edited_sections === 1 ? "1 section you edited" : `${doc.edited_sections} sections you edited`;
      if (!confirm(`Read the file again? This replaces the text of ${n}.`)) return;
    }
    try {
      setDoc(await api.reprocessDocument(documentId, mode));
      setError(null);
      if (mode === "extract") setSections(null);
      setEditingId(null);
    } catch (err) {
      report(err);
    }
  }

  if (!doc) {
    return (
      <div className="page">
        <div className="page-column">{error ? <p className="notice notice-error">{error}</p> : <p className="muted">Loading document…</p>}</div>
      </div>
    );
  }

  const shown = sections?.slice(0, visible) ?? [];
  const remaining = (sections?.length ?? 0) - shown.length;

  return (
    <div className="page">
      <div className="page-column doc-column">
        <header className="doc-header">
          <button className="back-link" onClick={onBack}>
            <ChevronLeftIcon width={16} height={16} /> {kb?.name ?? "Knowledge base"}
          </button>
          <h1 className="doc-title">{doc.filename}</h1>
          <div className="doc-summary">
            <span className={`type-tab type-${doc.file_type}`}>{fileTypeLabel(doc.file_type)}</span>
            {doc.section_count > 0 && <span>{countSections(doc.file_type, doc.section_count)}</span>}
            {doc.ocr_sections > 0 && <span>{countSections(doc.file_type, doc.ocr_sections)} read with OCR</span>}
            {doc.edited_sections > 0 && <span>{doc.edited_sections} edited</span>}
          </div>
          <p className="doc-intro">
            This is the text the assistant searches. Use Edit to fix OCR mistakes or add context; your changes are
            searchable as soon as you save.
          </p>
          <div className="doc-header-actions">
            <a className="btn" href={api.documentFileUrl(doc.id)} target="_blank" rel="noreferrer">
              <OpenIcon width={16} height={16} /> Open original file
            </a>
            <button className="btn btn-quiet" onClick={() => reprocess("extract")} disabled={processing}>
              Read the file again
            </button>
          </div>
        </header>

        {error && <p className="notice notice-error" role="alert">{error}</p>}
        {processing && (
          <p className="notice notice-progress" role="status">
            <span className="spinner" aria-hidden="true" /> {doc.progress ?? "Reading file…"}
          </p>
        )}
        {doc.status === "failed" && (
          <div className="notice notice-error notice-action" role="alert">
            <p>{doc.error}</p>
            <button className="btn btn-small" onClick={() => reprocess("auto")}>
              <RetryIcon width={15} height={15} /> Retry
            </button>
          </div>
        )}
        {doc.needs_reindex && (
          <div className="notice notice-action">
            <p>This document was indexed with a different embedding model ({doc.embedding_model}), so chat can't search it yet.</p>
            <button className="btn btn-small" onClick={() => reprocess("reindex")}>
              <RetryIcon width={15} height={15} /> Re-index
            </button>
          </div>
        )}

        {sections && (
          <div className="doc-sections">
            {shown.map((section) => (
              <SectionBlock
                key={section.id}
                section={section}
                fileType={doc.file_type}
                editing={editingId === section.id}
                runningOcr={ocrId === section.id}
                error={sectionError?.id === section.id ? sectionError.message : null}
                targeted={section.id === targetSectionId}
                locked={processing || (editingId !== null && editingId !== section.id) || ocrId !== null}
                onEdit={() => setEditingId(section.id)}
                onCancel={() => setEditingId(null)}
                onSave={(markdown) => save(section, markdown)}
                onOcr={() => runOcr(section)}
              />
            ))}
            {remaining > 0 && (
              <button className="btn show-more" onClick={() => setVisible((v) => v + BATCH)}>
                Show {Math.min(BATCH, remaining)} more ({countSections(doc.file_type, remaining)} left)
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

interface SectionProps {
  section: Section;
  fileType: string;
  editing: boolean;
  runningOcr: boolean;
  error: string | null;
  targeted: boolean;
  locked: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: (markdown: string) => Promise<void>;
  onOcr: () => void;
}

function SectionBlock({ section, fileType, editing, runningOcr, error, targeted, locked, ...actions }: SectionProps) {
  const label = section.location ?? (fileType === "image" ? "Image" : `Section ${section.idx + 1}`);
  const noun = countSections(fileType, 1).replace(/^1 /, "");

  return (
    <article id={`section-${section.id}`} className={`doc-section${targeted ? " is-target" : ""}${editing ? " is-editing" : ""}`}>
      <div className="section-margin">
        <span className="section-loc">{label}</span>
        {(section.origin === "ocr" || section.edited) && (
          <span className="section-tags">
            {section.origin === "ocr" && <span className="tag">OCR</span>}
            {section.edited && <span className="tag tag-edited">Edited</span>}
          </span>
        )}
        {!editing && (
          <span className="section-actions">
            <button className="link-btn" onClick={actions.onEdit} disabled={locked || runningOcr}>
              Edit
            </button>
            {section.can_ocr && (
              <button className="link-btn" onClick={actions.onOcr} disabled={locked || runningOcr}>
                {runningOcr ? "Running OCR…" : "Run OCR"}
              </button>
            )}
          </span>
        )}
      </div>

      <div className="section-body">
        {section.notice && !editing && <p className="section-notice">{section.notice}</p>}
        {error && <p className="section-notice" role="alert">{error}</p>}
        {editing ? (
          <SectionEditor initial={section.markdown} onSave={actions.onSave} onCancel={actions.onCancel} />
        ) : section.markdown ? (
          <div className="prose section-prose">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{section.markdown}</ReactMarkdown>
          </div>
        ) : (
          <p className="muted section-empty">No text on this {noun}.</p>
        )}
      </div>
    </article>
  );
}

function SectionEditor({
  initial,
  onSave,
  onCancel,
}: {
  initial: string;
  onSave: (markdown: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [tab, setTab] = useState<"write" | "preview">("write");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const dirty = value !== initial;

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(el.scrollHeight + 2, 160)}px`;
  }, [value, tab]);

  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  async function save() {
    if (!dirty || saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(value);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSaving(false);
    }
  }

  function cancel() {
    if (dirty && !confirm("Discard your changes to this section?")) return;
    onCancel();
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      save();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    }
  }

  return (
    <div className="editor" onKeyDown={handleKeyDown}>
      <div className="editor-tabs" role="tablist" aria-label="Editor view">
        <button role="tab" aria-selected={tab === "write"} onClick={() => setTab("write")}>
          Write
        </button>
        <button role="tab" aria-selected={tab === "preview"} onClick={() => setTab("preview")}>
          Preview
        </button>
      </div>
      <div className="editor-panes" data-tab={tab}>
        <label className="editor-write">
          <span className="editor-pane-label">Markdown</span>
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            spellCheck
            disabled={saving}
          />
        </label>
        <div className="editor-preview">
          <span className="editor-pane-label">Preview</span>
          <div className="prose">
            {value.trim() ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{value}</ReactMarkdown>
            ) : (
              <p className="muted">Nothing to preview. An empty section is left out of search.</p>
            )}
          </div>
        </div>
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="editor-actions">
        <span className="editor-hint">Ctrl+Enter saves, Esc cancels</span>
        <button className="btn btn-quiet" onClick={cancel} disabled={saving}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={save} disabled={!dirty || saving}>
          {saving ? "Saving and re-indexing…" : "Save changes"}
        </button>
      </div>
    </div>
  );
}
