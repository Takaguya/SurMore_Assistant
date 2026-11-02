import { api } from "./api";

// On serverless hosts (Vercel) the API processes a document in rounds of a few minutes, and the browser starts
// each one: while the app is open, a document still processing gets its next round as soon as the last ends.
// Closing the app pauses processing; it carries on when the app is opened again.

const CHECK_MS = 3000;

let enabled = false;
let running = false;
const inFlight = new Set<string>();

export function enableProcessingRounds(on: boolean) {
  enabled = on;
  kickProcessing();
}

/** Call after starting processing (upload, retry); harmless when there's nothing to do. */
export function kickProcessing() {
  if (enabled && !running) void drive();
}

async function drive() {
  running = true;
  try {
    for (;;) {
      const documents = await api.listProcessing();
      if (!documents.length && !inFlight.size) return;
      for (const document of documents) {
        if (document.busy || inFlight.has(document.id)) continue;
        inFlight.add(document.id);
        // A round that fails or times out leaves the document as it was; the next check starts another.
        api
          .processDocument(document.id)
          .catch(() => {})
          .finally(() => inFlight.delete(document.id));
      }
      await new Promise((resolve) => setTimeout(resolve, CHECK_MS));
    }
  } catch {
    // Offline or signed out: the next kick (an upload, a retry, opening the app) starts again.
  } finally {
    running = false;
  }
}
