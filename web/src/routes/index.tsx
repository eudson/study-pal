import { createFileRoute } from "@tanstack/react-router";
import { ApiHealthIndicator } from "../components/ApiHealthIndicator";

export const Route = createFileRoute("/")({
  component: IndexPage,
});

function IndexPage() {
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem", maxWidth: "40rem" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>StudyPal</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        Paper-first diagnostic learning, from scope to study pack.
      </p>
      <ApiHealthIndicator />
    </main>
  );
}
