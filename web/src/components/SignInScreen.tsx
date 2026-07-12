import { useState } from "react";

import { useAuth } from "../lib/auth";
import { StickerButton } from "./StickerButton";
import styles from "./SignInScreen.module.css";

/**
 * Unauthenticated landing. StudyPal is parent-gated: only a parent signs in
 * (with Google), and every child-visible step flows from that account.
 *
 * Visual design: "Sticker & Stamp" identity — paper canvas, Fredoka wordmark,
 * Atkinson tagline, coral StickerButton primary CTA.
 * All values via design tokens (tokens.css). No hardcoded colours or sizes.
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
    <main className={styles.canvas}>
      <div className={styles.brand}>
        <h1 className={styles.wordmark}>StudyPal</h1>
        <p className={styles.tagline}>
          Paper-first diagnostic learning, from scope to study pack.
        </p>
      </div>

      <StickerButton
        onClick={() => void onSignIn()}
        disabled={busy}
        aria-busy={busy}
      >
        {busy ? "Redirecting…" : "Sign in with Google"}
      </StickerButton>

      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
    </main>
  );
}
