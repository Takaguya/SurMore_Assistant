import type { Conversation, KnowledgeBase } from "../types";
import { formatRelative } from "../format";
import { useAccount } from "./AuthGate";
import { PlusIcon, TrashIcon } from "./Icons";

interface Props {
  open: boolean;
  conversations: Conversation[];
  knowledgeBases: KnowledgeBase[];
  activeConversationId: string | null;
  activeKbId: string | null;
  onNewChat: () => void;
  onOpenConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  onOpenKb: (id: string) => void;
  onCreateKb: () => void;
}

export function Sidebar(props: Props) {
  const { conversations, knowledgeBases, activeConversationId, activeKbId } = props;
  const account = useAccount();

  return (
    <nav className={`sidebar${props.open ? " open" : ""}`} aria-label="Chats and knowledge bases">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true" />
        SurMore Assistant
      </div>

      <button className="btn btn-primary new-chat" onClick={props.onNewChat}>
        <PlusIcon /> New chat
      </button>

      <section className="side-section">
        <div className="side-heading">
          <h2>Knowledge bases</h2>
          <button className="icon-btn" onClick={props.onCreateKb} aria-label="Create knowledge base" title="Create knowledge base">
            <PlusIcon />
          </button>
        </div>
        {knowledgeBases.length === 0 ? (
          <p className="side-empty">None yet. Create one to upload files.</p>
        ) : (
          <ul className="side-list">
            {knowledgeBases.map((kb) => (
              <li key={kb.id}>
                <button
                  className="side-item"
                  aria-current={kb.id === activeKbId ? "page" : undefined}
                  onClick={() => props.onOpenKb(kb.id)}
                >
                  <span className="side-item-title">{kb.name}</span>
                  <span className="side-item-meta">
                    {kb.document_count === 1 ? "1 file" : `${kb.document_count} files`}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="side-section side-section-grow">
        <div className="side-heading">
          <h2>Chats</h2>
        </div>
        {conversations.length === 0 ? (
          <p className="side-empty">Your chats will appear here.</p>
        ) : (
          <ul className="side-list">
            {conversations.map((c) => (
              <li key={c.id} className="side-row">
                <button
                  className="side-item"
                  aria-current={c.id === activeConversationId ? "page" : undefined}
                  onClick={() => props.onOpenConversation(c.id)}
                >
                  <span className="side-item-title">{c.title}</span>
                  <span className="side-item-meta">{formatRelative(c.updated_at)}</span>
                </button>
                <button
                  className="icon-btn side-delete"
                  onClick={() => props.onDeleteConversation(c.id)}
                  aria-label={`Delete chat “${c.title}”`}
                  title="Delete chat"
                >
                  <TrashIcon width={16} height={16} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {account.signOut && (
        <div className="account">
          <span className="account-email" title={account.email ?? undefined}>
            {account.email ?? "Signed in"}
          </span>
          <button className="btn btn-small btn-quiet" onClick={account.signOut}>
            Sign out
          </button>
        </div>
      )}
    </nav>
  );
}
