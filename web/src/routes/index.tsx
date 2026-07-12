import { createFileRoute } from "@tanstack/react-router";

import { ApiHealthIndicator } from "../components/ApiHealthIndicator";
import { useAuth } from "../lib/auth";

export const Route = createFileRoute("/")({
  component: IndexPage,
});

function IndexPage() {
  const { user, signOut } = useAuth();

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem", maxWidth: "40rem" }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: "1rem",
          marginBottom: "1.5rem",
        }}
      >
        <div>
          <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>StudyPal</h1>
          <p style={{ color: "#555" }}>
            Paper-first diagnostic learning, from scope to study pack.
          </p>
        </div>
        <div style={{ textAlign: "right", whiteSpace: "nowrap" }}>
          {user?.email && (
            <div style={{ color: "#777", fontSize: "0.85rem", marginBottom: "0.25rem" }}>
              {user.email}
            </div>
          )}
          <button
            type="button"
            onClick={() => void signOut()}
            style={{
              padding: "0.4rem 0.9rem",
              fontSize: "0.85rem",
              borderRadius: "0.375rem",
              border: "1px solid #ccc",
              background: "#fff",
              cursor: "pointer",
            }}
          >
            Sign out
          </button>
        </div>
      </div>
      <ApiHealthIndicator />
    </main>
  );
}
