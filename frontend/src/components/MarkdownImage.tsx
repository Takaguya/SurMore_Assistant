import type { ComponentProps } from "react";

/**
 * Images in document text and answers are shown as links instead of being loaded. Loading them would let a
 * document with hidden instructions get the model to write an image whose address carries text from your
 * other documents to someone else's server, fetched the moment the answer appears.
 */
export function MarkdownImage({ src, alt }: ComponentProps<"img">) {
  const label = `Image${alt ? `: ${alt}` : ""}`;
  if (typeof src !== "string" || !src) return <>{label}</>;
  return (
    <a href={src} target="_blank" rel="noreferrer">
      {label}
    </a>
  );
}

export const MARKDOWN_COMPONENTS = { img: MarkdownImage };
