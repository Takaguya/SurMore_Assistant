export const ACCEPTED_EXTENSIONS = [
  ".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv", ".txt", ".md",
  ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
];

const TYPE_LABELS: Record<string, string> = {
  pdf: "PDF",
  docx: "Word",
  pptx: "Slides",
  xlsx: "Excel",
  csv: "CSV",
  txt: "Text",
  md: "Markdown",
  image: "Image",
};

const SECTION_NOUNS: Record<string, [string, string]> = {
  pdf: ["page", "pages"],
  pptx: ["slide", "slides"],
  xlsx: ["table", "tables"],
  csv: ["table", "tables"],
  image: ["image", "images"],
};

/** "12 pages", "1 slide", "4 sections": what a document's sections are called for its file type. */
export function countSections(type: string, count: number): string {
  const [one, many] = SECTION_NOUNS[type] ?? ["section", "sections"];
  return `${count} ${count === 1 ? one : many}`;
}

export function fileTypeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type.toUpperCase();
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

export function formatRelative(iso: string): string {
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  const steps: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31536000],
    ["month", 2592000],
    ["week", 604800],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, size] of steps) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

/** Split reasoning-model output (<think>…</think>) from the visible answer. */
export function splitThinking(content: string): { thinking: string; answer: string; thinkingOpen: boolean } {
  const start = content.indexOf("<think>");
  if (start === -1) return { thinking: "", answer: content, thinkingOpen: false };
  const end = content.indexOf("</think>", start);
  if (end === -1) {
    return { thinking: content.slice(start + 7).trim(), answer: content.slice(0, start), thinkingOpen: true };
  }
  return {
    thinking: content.slice(start + 7, end).trim(),
    answer: (content.slice(0, start) + content.slice(end + 8)).trim(),
    thinkingOpen: false,
  };
}
