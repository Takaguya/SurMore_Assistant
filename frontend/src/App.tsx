import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { ChatView } from "./components/ChatView";
import { DocumentView } from "./components/DocumentView";
import { MenuIcon } from "./components/Icons";
import { KbFormDialog } from "./components/KbFormDialog";
import { KnowledgeBaseView } from "./components/KnowledgeBaseView";
import { Sidebar } from "./components/Sidebar";
import type { Conversation, Health, KnowledgeBase, Source } from "./types";

type View =
  | { kind: "chat"; key: number; conversationId: string | null; kbIds: string[] }
  | { kind: "kb"; kbId: string }
  | { kind: "doc"; key: number; docId: string; kbId: string; sectionId: string | null };

const SELECTION_KEY = "surmore.kbSelection";

function readSelection(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(SELECTION_KEY) ?? "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

function saveSelection(ids: string[]) {
  try {
    localStorage.setItem(SELECTION_KEY, JSON.stringify(ids));
  } catch {
    // storage unavailable (private mode); selection just won't be remembered
  }
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [backendDown, setBackendDown] = useState(false);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [view, setView] = useState<View>(() => ({ kind: "chat", key: 0, conversationId: null, kbIds: readSelection() }));
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [creatingKb, setCreatingKb] = useState(false);
  const viewKey = useRef(0);

  const refreshKnowledgeBases = useCallback(async () => {
    try {
      setKnowledgeBases(await api.listKnowledgeBases());
    } catch {
      setBackendDown(true);
    }
  }, []);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.listConversations());
    } catch {
      setBackendDown(true);
    }
  }, []);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch(() => setBackendDown(true));
    refreshKnowledgeBases();
    refreshConversations();
  }, [refreshKnowledgeBases, refreshConversations]);

  function openChat(conversationId: string | null, kbIds: string[] = readSelection()) {
    viewKey.current += 1;
    setView({ kind: "chat", key: viewKey.current, conversationId, kbIds });
    setActiveConversationId(conversationId);
    setSidebarOpen(false);
  }

  function openKb(kbId: string) {
    setView({ kind: "kb", kbId });
    setActiveConversationId(null);
    setSidebarOpen(false);
  }

  function openDocument(docId: string, kbId: string, sectionId: string | null = null) {
    viewKey.current += 1;
    setView({ kind: "doc", key: viewKey.current, docId, kbId, sectionId });
    setActiveConversationId(null);
    setSidebarOpen(false);
  }

  const openSource = useCallback(
    (source: Source) => openDocument(source.document_id, source.kb_id, source.section_id),
    [],
  );

  async function deleteConversation(id: string) {
    const conversation = conversations.find((c) => c.id === id);
    if (!confirm(`Delete the chat “${conversation?.title ?? "Untitled"}”?`)) return;
    await api.deleteConversation(id);
    await refreshConversations();
    if (id === activeConversationId) openChat(null);
  }

  const handleSelectionChange = useCallback((ids: string[]) => saveSelection(ids), []);
  const handleConversationCreated = useCallback(
    (c: Conversation) => {
      setActiveConversationId(c.id);
      setConversations((cs) => [c, ...cs]);
    },
    [],
  );

  const activeKb = view.kind !== "chat" ? knowledgeBases.find((kb) => kb.id === view.kbId) : undefined;
  const missing = health
    ? [!health.api_key_configured && "NVIDIA_API_KEY", !health.llm_model && "NVIDIA_LLM_MODEL"].filter(Boolean)
    : [];

  const pageTitle =
    view.kind !== "chat"
      ? (activeKb?.name ?? "Knowledge base")
      : (conversations.find((c) => c.id === activeConversationId)?.title ?? "New chat");

  return (
    <div className="app">
      <Sidebar
        open={sidebarOpen}
        conversations={conversations}
        knowledgeBases={knowledgeBases}
        activeConversationId={view.kind === "chat" ? activeConversationId : null}
        activeKbId={view.kind !== "chat" ? view.kbId : null}
        onNewChat={() => openChat(null)}
        onOpenConversation={(id) => openChat(id)}
        onDeleteConversation={deleteConversation}
        onOpenKb={openKb}
        onCreateKb={() => setCreatingKb(true)}
      />
      {sidebarOpen && <div className="scrim" onClick={() => setSidebarOpen(false)} aria-hidden="true" />}

      <main className="main">
        <header className="topbar">
          <button className="icon-btn" onClick={() => setSidebarOpen(true)} aria-label="Open menu">
            <MenuIcon />
          </button>
          <span className="topbar-title">{pageTitle}</span>
        </header>

        {backendDown && (
          <div className="banner" role="alert">
            Can't reach the SurMore backend. In the <code>backend</code> folder run{" "}
            <code>uvicorn app.main:app --reload</code>, then reload this page.
          </div>
        )}
        {missing.length > 0 && (
          <div className="banner" role="alert">
            Add {missing.map((m, i) => (
              <span key={String(m)}>
                {i > 0 && " and "}
                <code>{m}</code>
              </span>
            ))}{" "}
            to <code>backend/.env</code>, then restart the backend.
          </div>
        )}

        {view.kind === "chat" ? (
          <ChatView
            key={view.key}
            conversationId={view.conversationId}
            knowledgeBases={knowledgeBases}
            initialKbIds={view.kbIds}
            onConversationCreated={handleConversationCreated}
            onConversationActivity={refreshConversations}
            onSelectionChange={handleSelectionChange}
            onCreateKb={() => setCreatingKb(true)}
            onOpenSource={openSource}
          />
        ) : view.kind === "doc" ? (
          <DocumentView
            key={view.key}
            documentId={view.docId}
            kb={activeKb}
            targetSectionId={view.sectionId}
            onBack={() => openKb(view.kbId)}
            onChanged={refreshKnowledgeBases}
          />
        ) : activeKb ? (
          <KnowledgeBaseView
            key={activeKb.id}
            kb={activeKb}
            maxUploadMb={health?.max_upload_mb}
            onChanged={refreshKnowledgeBases}
            onDeleted={async () => {
              await refreshKnowledgeBases();
              openChat(null);
            }}
            onOpenDocument={(docId) => openDocument(docId, activeKb.id)}
            onStartChat={(kbId) => {
              saveSelection([kbId]);
              openChat(null, [kbId]);
            }}
          />
        ) : (
          <div className="page">
            <p className="muted page-column">This knowledge base no longer exists.</p>
          </div>
        )}
      </main>

      <KbFormDialog
        open={creatingKb}
        title="Create knowledge base"
        submitLabel="Create knowledge base"
        onClose={() => setCreatingKb(false)}
        onSubmit={async (name, description) => {
          const kb = await api.createKnowledgeBase(name, description);
          setCreatingKb(false);
          await refreshKnowledgeBases();
          openKb(kb.id);
        }}
      />
    </div>
  );
}
