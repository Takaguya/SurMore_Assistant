import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";
import type { Conversation, KnowledgeBase, Source } from "../types";
import { SendIcon, StopIcon } from "./Icons";
import { MessageView, type UIMessage } from "./MessageView";

interface Props {
  conversationId: string | null;
  knowledgeBases: KnowledgeBase[];
  initialKbIds: string[];
  onConversationCreated: (conversation: Conversation) => void;
  onConversationActivity: () => void;
  onSelectionChange: (kbIds: string[]) => void;
  onCreateKb: () => void;
  onOpenSource: (source: Source) => void;
}

let tempCounter = 0;
const tempId = () => `temp-${++tempCounter}`;

export function ChatView(props: Props) {
  const { knowledgeBases } = props;
  const [conversationId, setConversationId] = useState(props.conversationId);
  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [kbIds, setKbIds] = useState<string[]>(props.initialKbIds);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(props.conversationId !== null);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const selected = kbIds.filter((id) => knowledgeBases.some((kb) => kb.id === id));

  useEffect(() => {
    if (!props.conversationId) return;
    api
      .getConversation(props.conversationId)
      .then((c) => {
        setMessages(c.messages);
        setKbIds(c.kb_ids);
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [props.conversationId]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [input]);

  function toggleKb(id: string) {
    const next = selected.includes(id) ? selected.filter((k) => k !== id) : [...selected, id];
    setKbIds(next);
    props.onSelectionChange(next);
    if (conversationId) api.updateConversation(conversationId, { kb_ids: next }).catch(() => {});
  }

  function patch(id: string, update: (m: UIMessage) => UIMessage) {
    setMessages((ms) => ms.map((m) => (m.id === id ? update(m) : m)));
  }

  async function send(question: string) {
    if (!question || streaming) return;
    const userId = tempId();
    const assistantId = tempId();
    const now = new Date().toISOString();
    stickToBottom.current = true;
    setMessages((ms) => [
      ...ms,
      { id: userId, role: "user", content: question, sources: [], created_at: now },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        sources: [],
        created_at: now,
        status: selected.length ? "searching" : "writing",
      },
    ]);
    setStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      let id = conversationId;
      if (!id) {
        const conversation = await api.createConversation(selected);
        id = conversation.id;
        setConversationId(id);
        props.onConversationCreated(conversation);
      }
      await api.streamMessage(
        id,
        question,
        selected,
        {
          onSources: (sources) => patch(assistantId, (m) => ({ ...m, sources, status: "writing" })),
          onToken: (text) => patch(assistantId, (m) => ({ ...m, content: m.content + text })),
          onDone: ({ user_message, assistant_message }) =>
            setMessages((ms) =>
              ms.map((m) => (m.id === userId ? user_message : m.id === assistantId ? assistant_message : m)),
            ),
          onError: (message) => patch(assistantId, (m) => ({ ...m, status: undefined, error: message })),
        },
        controller.signal,
      );
    } catch (err) {
      if (!controller.signal.aborted) {
        const message = err instanceof Error ? err.message : String(err);
        patch(assistantId, (m) => ({ ...m, status: undefined, error: message }));
      }
    } finally {
      patch(assistantId, (m) => ({ ...m, status: undefined }));
      setStreaming(false);
      abortRef.current = null;
      props.onConversationActivity();
    }
  }

  function retry(failedAssistantId: string) {
    const index = messages.findIndex((m) => m.id === failedAssistantId);
    const question = messages[index - 1];
    if (!question || question.role !== "user") return;
    setMessages((ms) => ms.filter((m) => m.id !== failedAssistantId && m.id !== question.id));
    send(question.content);
  }

  function submit(e?: FormEvent) {
    e?.preventDefault();
    const question = input.trim();
    if (!question) return;
    setInput("");
    send(question);
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="chat">
      <div
        className="chat-scroll"
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        <div className="chat-column">
          {loading ? (
            <p className="muted">Loading chat…</p>
          ) : loadError ? (
            <p className="notice notice-error">{loadError}</p>
          ) : messages.length === 0 ? (
            <EmptyChat hasKbs={knowledgeBases.length > 0} onCreateKb={props.onCreateKb} />
          ) : (
            <div className="messages" aria-live="polite">
              {messages.map((m) => (
                <MessageView
                  key={m.id}
                  message={m}
                  onRetry={m.error ? () => retry(m.id) : undefined}
                  onOpenSource={props.onOpenSource}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      <form className="composer-area" onSubmit={submit}>
        <div className="composer-column">
          {knowledgeBases.length > 0 && (
            <div className="kb-picker" role="group" aria-labelledby="kb-picker-label">
              <span id="kb-picker-label" className="kb-picker-label">
                Answer from
              </span>
              {knowledgeBases.map((kb) => (
                <button
                  key={kb.id}
                  type="button"
                  className="kb-chip"
                  aria-pressed={selected.includes(kb.id)}
                  onClick={() => toggleKb(kb.id)}
                  disabled={streaming}
                  title={kb.ready_count === 0 ? "No ready files in this knowledge base yet" : kb.description || undefined}
                >
                  {kb.name}
                  {kb.ready_count === 0 && <span className="kb-chip-note">empty</span>}
                </button>
              ))}
            </div>
          )}
          {knowledgeBases.length > 0 && selected.length === 0 && (
            <p className="kb-picker-hint">No knowledge base selected, so answers come from the model's general knowledge.</p>
          )}
          <div className="composer">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={selected.length ? "Ask about your documents" : "Ask anything"}
              rows={1}
              aria-label="Message"
            />
            {streaming ? (
              <button type="button" className="send-btn" onClick={() => abortRef.current?.abort()} aria-label="Stop answering">
                <StopIcon />
              </button>
            ) : (
              <button type="submit" className="send-btn" disabled={!input.trim()} aria-label="Send message">
                <SendIcon />
              </button>
            )}
          </div>
        </div>
      </form>
    </div>
  );
}

function EmptyChat({ hasKbs, onCreateKb }: { hasKbs: boolean; onCreateKb: () => void }) {
  return (
    <div className="chat-empty">
      <h1>What do you want to know?</h1>
      <p>
        Pick the knowledge bases to answer from, then ask a question. Every answer marks the passages it used, like
        this <span className="cite cite-static">1</span>, so you can check the original.
      </p>
      {!hasKbs && (
        <p>
          You don't have any knowledge bases yet.{" "}
          <button className="link-btn" onClick={onCreateKb}>
            Create one and upload your files
          </button>
          , or chat without one.
        </p>
      )}
    </div>
  );
}
