import { useState } from "react";

import { useAuth } from "../lib/auth";

/**
 * Unauthenticated landing. StudyPal is parent-gated: only a parent signs in
 * (with Google), and every child-visible step flows from that account. This is
 * the shell — visual design lands once the design tokens are locked.
 */
export function SignInScreen() {
  const { signInWithGoogle } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSignIn = async () => {
    setBusy(true);
    setError(null);
    try {
      await signInWithGoogle();
      // On success the browser redirects to Google; nothing renders after.
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed. Please try again.");
      setBusy(false);
    }
  };

  return (
    <main
      style={{
        fontFamily: "system-ui, sans-serif",
        minHeight: "100dvh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "1.25rem",
        padding: "2rem",
        textAlign: "center",
      }}
    >
      <div>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>StudyPal</h1>
        <p style={{ color: "#555", maxWidth: "28rem" }}>
          Paper-first diagnostic learning, from scope to study pack.
        </p>
      </div>

      <button
        type="button"
        onClick={() => void onSignIn()}
        disabled={busy}
        style={{
          padding: "0.75rem 1.5rem",
          fontSize: "1rem",
          borderRadius: "0.5rem",
          border: "1px solid #ccc",
          background: "#fff",
          cursor: busy ? "default" : "pointer",
          opacity: busy ? 0.6 : 1,
        }}
      >
        {busy ? "Redirecting…" : "Sign in with Google"}
      </button>

      {error && (
        <p role="alert" style={{ color: "#c0392b" }}>
          {error}
        </p>
      )}
    </main>
  );
}
