import { memo, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import { fileTypeLabel, splitThinking } from "../format";
import type { Message, Source } from "../types";
import { OpenIcon, RetryIcon } from "./Icons";

export interface UIMessage extends Message {
  status?: "searching" | "writing";
  error?: string;
}

interface Props {
  message: UIMessage;
  onRetry?: () => void;
  onOpenSource: (source: Source) => void;
}

const CITATION = /\[(\d+(?:\s*[,;]\s*\d+)*)\](?!\()/g;

/** Turn [1], [2, 3] into links the Markdown renderer hands to <Citation>. */
function linkCitations(markdown: string, max: number): { text: string; cited: Set<number> } {
  const cited = new Set<number>();
  const text = markdown.replace(CITATION, (whole, list: string) => {
    const numbers = list.split(/\s*[,;]\s*/).map(Number);
    if (numbers.some((n) => n < 1 || n > max)) return whole;
    numbers.forEach((n) => cited.add(n));
    return numbers.map((n) => `[${n}](#cite-${n})`).join("");
  });
  return { text, cited };
}

export const MessageView = memo(function MessageView({ message, onRetry, onOpenSource }: Props) {
  const [openSource, setOpenSource] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);

  const { thinking, answer, thinkingOpen } = useMemo(() => splitThinking(message.content), [message.content]);
  const { text, cited } = useMemo(
    () => linkCitations(answer, message.sources.length),
    [answer, message.sources.length],
  );

  if (message.role === "user") {
    return (
      <div className="msg msg-user">
        <p>{message.content}</p>
      </div>
    );
  }

  function focusSource(n: number) {
    setOpenSource(n);
    if (!cited.has(n)) setShowAll(true);
    requestAnimationFrame(() =>
      document.getElementById(`src-${message.id}-${n}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" }),
    );
  }

  const citedSources = message.sources.filter((s) => cited.has(s.n));
  const otherSources = message.sources.filter((s) => !cited.has(s.n));
  const busy = message.status !== undefined;

  return (
    <div className="msg msg-assistant" aria-busy={busy}>
      {thinking && (
        <details className="thinking">
          <summary>{thinkingOpen ? "Thinking…" : "Show reasoning"}</summary>
          <p>{thinking}</p>
        </details>
      )}

      {answer ? (
        <div className="prose">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ href, children }) => {
                const match = href?.match(/^#cite-(\d+)$/);
                if (match) {
                  const n = Number(match[1]);
                  return (
                    <button type="button" className="cite" onClick={() => focusSource(n)} aria-label={`Show source ${n}`}>
                      {n}
                    </button>
                  );
                }
                return (
                  <a href={href} target="_blank" rel="noreferrer">
                    {children}
                  </a>
                );
              },
            }}
          >
            {text}
          </ReactMarkdown>
        </div>
      ) : (
        busy &&
        !thinking && (
          <p className="typing">
            <span className="typing-dots" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            {message.status === "searching" ? "Searching your knowledge bases" : "Writing"}
          </p>
        )
      )}

      {message.error && (
        <div className="msg-error" role="alert">
          <p>{message.error}</p>
          {onRetry && (
            <button className="btn btn-small" onClick={onRetry}>
              <RetryIcon width={15} height={15} /> Try again
            </button>
          )}
        </div>
      )}

      {message.sources.length > 0 && !busy && (
        <div className="sources">
          {citedSources.length > 0 && (
            <ol className="source-list" aria-label="Sources cited in this answer">
              {citedSources.map((s) => (
                <SourceItem
                  key={s.n}
                  id={`src-${message.id}-${s.n}`}
                  source={s}
                  cited
                  open={openSource === s.n}
                  onToggle={() => setOpenSource(openSource === s.n ? null : s.n)}
                  onOpenSource={onOpenSource}
                />
              ))}
            </ol>
          )}
          {otherSources.length > 0 && (
            <>
              <button className="link-btn" onClick={() => setShowAll(!showAll)} aria-expanded={showAll}>
                {showAll
                  ? "Hide other passages"
                  : citedSources.length
                    ? `${otherSources.length} more ${otherSources.length === 1 ? "passage was" : "passages were"} searched but not cited`
                    : `Show the ${otherSources.length} ${otherSources.length === 1 ? "passage" : "passages"} that were searched`}
              </button>
              {showAll && (
                <ol className="source-list source-list-other">
                  {otherSources.map((s) => (
                    <SourceItem
                      key={s.n}
                      id={`src-${message.id}-${s.n}`}
                      source={s}
                      cited={false}
                      open={openSource === s.n}
                      onToggle={() => setOpenSource(openSource === s.n ? null : s.n)}
                      onOpenSource={onOpenSource}
                    />
                  ))}
                </ol>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
});

function SourceItem({
  id,
  source,
  cited,
  open,
  onToggle,
  onOpenSource,
}: {
  id: string;
  source: Source;
  cited: boolean;
  open: boolean;
  onToggle: () => void;
  onOpenSource: (source: Source) => void;
}) {
  return (
    <li id={id} className={`source${cited ? " source-cited" : ""}${open ? " source-open" : ""}`}>
      <button className="source-head" onClick={onToggle} aria-expanded={open}>
        <span className="source-n">{source.n}</span>
        <span className="source-file">{source.filename}</span>
        {source.location && <span className="source-loc">{source.location}</span>}
      </button>
      {open && (
        <div className="source-body">
          <p className="source-passage">
            <mark>{source.content}</mark>
          </p>
          <span className="source-links">
          {source.section_id && (
            <button className="source-open-link link-plain" onClick={() => onOpenSource(source)}>
              Show in document
            </button>
          )}
          <a
            className="source-open-link"
            href={api.documentFileUrl(source.document_id, source.file_type, source.location)}
            target="_blank"
            rel="noreferrer"
          >
            <OpenIcon width={15} height={15} /> Open {fileTypeLabel(source.file_type)} file
            {source.file_type === "pdf" && source.location?.startsWith("Page") ? ` at ${source.location.toLowerCase()}` : ""}
          </a>
          </span>
        </div>
      )}
    </li>
  );
}
