import { useQuery } from "@tanstack/react-query";
import { getHealth } from "../api";
import type { HealthStatus } from "../api";

async function fetchHealth(): Promise<HealthStatus> {
  // `throwOnError: true` makes the SDK reject the promise on a non-2xx
  // response or network failure, so TanStack Query can treat it as an error
  // without us having to hand-narrow the generated response union.
  const { data } = await getHealth({ throwOnError: true });
  return data;
}

export function ApiHealthIndicator() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 10000,
  });

  const dotColor = isPending ? "#999" : isError || data?.status !== "ok" ? "#c0392b" : "#27ae60";
  const label = isPending
    ? "checking…"
    : isError || data?.status !== "ok"
      ? "API unreachable"
      : "API healthy";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
      <span
        aria-hidden="true"
        style={{
          display: "inline-block",
          width: "0.75rem",
          height: "0.75rem",
          borderRadius: "9999px",
          backgroundColor: dotColor,
        }}
      />
      <span>{label}</span>
    </div>
  );
}
