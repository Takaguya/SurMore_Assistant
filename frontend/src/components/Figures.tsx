import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useMediaToken } from "../auth";
import type { Figure } from "../types";

export interface FigureItem {
  figure: Figure;
  /** Where the figure comes from, e.g. "Page 201" */
  location: string;
  filename?: string;
  /** Number of the source it belongs to, shown on the thumbnail */
  sourceN?: number;
  onShowInDocument?: () => void;
}

/** A row of diagram thumbnails; clicking one opens it large. */
export function FigureStrip({ items, label, caption }: { items: FigureItem[]; label: string; caption?: string }) {
  const [open, setOpen] = useState<FigureItem | null>(null);
  // Figures that failed to load (e.g. the file was deleted) are hidden rather than shown as empty boxes.
  const [broken, setBroken] = useState<ReadonlySet<string>>(new Set());
  const mediaToken = useMediaToken();
  const shown = items.filter((item) => !broken.has(item.figure.id));
  if (!shown.length) return null;

  return (
    <div className="figures">
      {caption && <p className="figures-caption">{caption}</p>}
      <ul className="figure-strip" aria-label={label}>
        {shown.map((item) => (
          <li key={item.figure.id}>
            <button
              type="button"
              className="figure-thumb"
              onClick={() => setOpen(item)}
              aria-label={`Enlarge the diagram from ${item.location}`}
            >
              <img
                src={api.figureUrl(item.figure.id, mediaToken)}
                width={item.figure.width}
                height={item.figure.height}
                alt=""
                loading="lazy"
                decoding="async"
                onError={() => setBroken((prev) => new Set(prev).add(item.figure.id))}
              />
              {item.sourceN !== undefined && <span className="figure-n">{item.sourceN}</span>}
            </button>
          </li>
        ))}
      </ul>
      {open && <FigureViewer item={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function FigureViewer({ item, onClose }: { item: FigureItem; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const url = api.figureUrl(item.figure.id, useMediaToken());

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  const close = () => ref.current?.close();

  return (
    <dialog
      ref={ref}
      className="figure-viewer"
      onClose={onClose}
      // A click on the dialog itself (not its content) is a click on the backdrop.
      onClick={(e) => e.target === e.currentTarget && close()}
      aria-label={`Diagram from ${item.location}`}
    >
      <div className="figure-viewer-inner">
        <img src={url} width={item.figure.width} height={item.figure.height} alt={`Diagram from ${item.location}`} />
        <div className="figure-viewer-bar">
          <p className="figure-viewer-where">
            {item.filename && <span className="figure-viewer-file">{item.filename}</span>}
            <span>{item.location}</span>
          </p>
          {item.onShowInDocument && (
            <button
              className="btn btn-small"
              onClick={() => {
                close();
                item.onShowInDocument?.();
              }}
            >
              Show in document
            </button>
          )}
          <a className="btn btn-small" href={url} target="_blank" rel="noreferrer">
            Open full size
          </a>
          <button className="btn btn-small btn-primary" onClick={close} autoFocus>
            Close
          </button>
        </div>
      </div>
    </dialog>
  );
}
