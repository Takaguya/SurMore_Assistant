import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { initSupabase, loadPublicConfig, syncTokenCookie, UNAUTHORIZED_EVENT, type PublicConfig } from "../auth";
import "../auth.css";

export interface Account {
  email: string | null;
  signOut: (() => Promise<void>) | null;
}

const AccountContext = createContext<Account>({ email: null, signOut: null });
export const useAccount = () => useContext(AccountContext);

const PROVIDER_LABELS: Record<string, string> = {
  google: "Google",
  github: "GitHub",
  azure: "Microsoft",
  apple: "Apple",
  gitlab: "GitLab",
  discord: "Discord",
};

type State =
  | { kind: "loading" }
  | { kind: "open" } // sign-in is off (AUTH_MODE=none) or the backend is unreachable
  | { kind: "supabase"; config: PublicConfig; client: SupabaseClient; session: Session | null };

export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let unsubscribe = () => {};
    loadPublicConfig()
      .then(async (config) => {
        if (config.auth_mode !== "supabase") {
          setState({ kind: "open" });
          return;
        }
        const client = await initSupabase(config);
        const { data } = await client.auth.getSession();
        syncTokenCookie(data.session);
        setState({ kind: "supabase", config, client, session: data.session });
        const { data: listener } = client.auth.onAuthStateChange((_event, session) => {
          syncTokenCookie(session);
          setState((s) => (s.kind === "supabase" ? { ...s, session } : s));
        });
        unsubscribe = () => listener.subscription.unsubscribe();
        // Remove the one-time ?code= from the address bar after a sign-in redirect.
        if (new URLSearchParams(location.search).has("code")) history.replaceState(null, "", location.pathname);
      })
      // The app itself explains that the backend is unreachable.
      .catch(() => setState({ kind: "open" }));
    return () => unsubscribe();
  }, []);

  const client = state.kind === "supabase" ? state.client : null;
  useEffect(() => {
    if (!client) return;
    // The server rejected the token: try a refresh, and if that fails the sign-in screen comes back.
    const onUnauthorized = async () => {
      const { error } = await client.auth.refreshSession();
      if (error) await client.auth.signOut();
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [client]);

  if (state.kind === "loading") return <div className="auth-loading" aria-busy="true" />;
  if (state.kind === "open") {
    return <AccountContext.Provider value={{ email: null, signOut: null }}>{children}</AccountContext.Provider>;
  }
  if (!state.session) return <SignIn client={state.client} providers={state.config.auth_providers} />;

  const account: Account = {
    email: state.session.user.email ?? null,
    signOut: async () => {
      await state.client.auth.signOut();
    },
  };
  // Keyed by user so nothing from a previous account survives a switch.
  return (
    <AccountContext.Provider value={account} key={state.session.user.id}>
      {children}
    </AccountContext.Provider>
  );
}

function SignIn({ client, providers }: { client: SupabaseClient; providers: string[] }) {
  const [email, setEmail] = useState("");
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const oauthProviders = providers.filter((p) => p !== "email");
  const emailEnabled = providers.includes("email");
  const redirectTo = `${location.origin}${location.pathname}`;

  async function sendLink(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const { error } = await client.auth.signInWithOtp({ email: email.trim(), options: { emailRedirectTo: redirectTo } });
    setBusy(false);
    if (error) setError(error.message);
    else setSentTo(email.trim());
  }

  async function signInWith(provider: string) {
    setError(null);
    const { error } = await client.auth.signInWithOAuth({
      provider: provider as Parameters<typeof client.auth.signInWithOAuth>[0]["provider"],
      options: { redirectTo },
    });
    if (error) setError(error.message);
  }

  return (
    <main className="signin">
      <div className="signin-panel">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          SurMore Assistant
        </div>

        {sentTo ? (
          <>
            <h1>Check your email</h1>
            <p className="signin-lead">
              We sent a sign-in link to <strong>{sentTo}</strong>. Open it in this browser to continue.
            </p>
            <button className="link-btn" onClick={() => setSentTo(null)}>
              Use a different email
            </button>
          </>
        ) : (
          <>
            <h1>Sign in</h1>
            <p className="signin-lead">Your knowledge bases and chats are private to your account.</p>

            {oauthProviders.map((provider) => (
              <button key={provider} className="btn signin-provider" onClick={() => signInWith(provider)}>
                Continue with {PROVIDER_LABELS[provider] ?? provider}
              </button>
            ))}

            {emailEnabled && oauthProviders.length > 0 && <p className="signin-or">or</p>}

            {emailEnabled && (
              <form onSubmit={sendLink} className="signin-form">
                <label className="field">
                  <span>Email</span>
                  <input
                    type="email"
                    required
                    autoComplete="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@example.com"
                  />
                </label>
                <button type="submit" className="btn btn-primary" disabled={busy || !email.trim()}>
                  {busy ? "Sending…" : "Email me a sign-in link"}
                </button>
              </form>
            )}
          </>
        )}

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </main>
  );
}
