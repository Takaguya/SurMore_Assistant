import type { Session, SupabaseClient } from "@supabase/supabase-js";

// Same base as api.ts: empty in development (Vite proxies /api).
const BASE = import.meta.env.VITE_API_URL ?? "";

export interface PublicConfig {
  auth_mode: "none" | "supabase";
  supabase_url: string;
  supabase_anon_key: string;
  auth_providers: string[];
}

// Opening an original file is a plain link, which can't send an Authorization header,
// so the access token is mirrored into a cookie the API accepts for GET requests only.
const TOKEN_COOKIE = "surmore_token";

let supabase: SupabaseClient | null = null;

export async function loadPublicConfig(): Promise<PublicConfig> {
  const res = await fetch(`${BASE}/api/public-config`);
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

export function syncTokenCookie(session: Session | null) {
  const secure = location.protocol === "https:" ? "; Secure" : "";
  document.cookie = session
    ? `${TOKEN_COOKIE}=${session.access_token}; Path=/api; SameSite=Strict; Max-Age=${session.expires_in}${secure}`
    : `${TOKEN_COOKIE}=; Path=/api; SameSite=Strict; Max-Age=0${secure}`;
}

/** Fired by the API client when the server rejects the sign-in, so the app can ask the user to sign in again. */
export const UNAUTHORIZED_EVENT = "surmore:unauthorized";
