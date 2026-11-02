import { useSyncExternalStore } from "react";
import type { Session, SupabaseClient } from "@supabase/supabase-js";

// Empty in development (Vite proxies /api). Set VITE_API_URL if the API lives on another host;
// a slash at the end is dropped, since every API path starts with one.
export const API_BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");

export interface PublicConfig {
  auth_mode: "none" | "supabase";
  supabase_url: string;
  supabase_anon_key: string;
  auth_providers: string[];
}

let supabase: SupabaseClient | null = null;

export async function loadPublicConfig(): Promise<PublicConfig> {
  const res = await fetch(`${API_BASE}/api/public-config`);
  if (!res.ok) throw new Error(`Couldn't load sign-in settings (${res.status})`);
  return res.json();
}

/** Loaded on demand, so the sign-in library isn't downloaded when sign-in is off. */
export async function initSupabase(config: PublicConfig): Promise<SupabaseClient> {
  const { createClient } = await import("@supabase/supabase-js");
  supabase ??= createClient(config.supabase_url, config.supabase_anon_key, {
    auth: { flowType: "pkce", persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
  });
  return supabase;
}

/** A current access token (refreshed if it expired), or null when sign-in is off. */
export async function getAccessToken(): Promise<string | null> {
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

// <img> tags and file links can't send the Authorization header, and with the API on another host no cookie
// reaches it either, so their URLs carry a short-lived media token from the API instead. It only opens this
// account's diagrams and original files.
let mediaToken: string | null = null;
let mediaTokenRun = 0;
let renewTimer: number | undefined;
const mediaTokenListeners = new Set<() => void>();

function subscribeMediaToken(listener: () => void) {
  mediaTokenListeners.add(listener);
  return () => {
    mediaTokenListeners.delete(listener);
  };
}

function setMediaToken(token: string | null) {
  mediaToken = token;
  mediaTokenListeners.forEach((listener) => listener());
}

/** The media token for diagram and file URLs (null when sign-in is off); re-renders when it's renewed. */
export function useMediaToken(): string | null {
  return useSyncExternalStore(subscribeMediaToken, () => mediaToken);
}

/** Get a media token for this session (or drop it on sign-out), and renew it shortly before it runs out. */
export async function syncMediaToken(session: Session | null): Promise<void> {
  const run = ++mediaTokenRun;
  window.clearTimeout(renewTimer);
  if (!session) {
    setMediaToken(null);
    return;
  }
  let renewInSeconds = 60; // try again soon if this attempt fails
  try {
    // The session's own token rather than getAccessToken(): this also runs inside onAuthStateChange,
    // where calling back into the Supabase client can deadlock.
    const res = await fetch(`${API_BASE}/api/media-token`, {
      headers: { Authorization: `Bearer ${session.access_token}` },
    });
    if (res.ok) {
      const { token, expires_in } = (await res.json()) as { token: string; expires_in: number };
      if (run !== mediaTokenRun) return; // signed out or in again meanwhile
      setMediaToken(token);
      renewInSeconds = Math.max(expires_in - 600, 60);
    }
  } catch {
    // Offline, or the API is down: diagrams and file links wait for the retry.
  }
  if (run !== mediaTokenRun) return;
  renewTimer = window.setTimeout(async () => {
    if (!supabase) return;
    const { data } = await supabase.auth.getSession(); // refreshes the access token first if it ran out
    await syncMediaToken(data.session);
  }, renewInSeconds * 1000);
}

/** Fired by the API client when the server rejects the sign-in, so the app can ask the user to sign in again. */
export const UNAUTHORIZED_EVENT = "surmore:unauthorized";
