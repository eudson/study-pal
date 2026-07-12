import { createRootRoute, Outlet } from "@tanstack/react-router";

import { SignInScreen } from "../components/SignInScreen";
import { useAuth } from "../lib/auth";

export const Route = createRootRoute({
  component: RootComponent,
});

function RootComponent() {
  const { loading, session } = useAuth();

  if (loading) {
    return (
      <main
        style={{
          fontFamily: "system-ui, sans-serif",
          minHeight: "100dvh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#777",
        }}
      >
        Loading…
      </main>
    );
  }

  // Auth gate: no session → sign-in shell; otherwise the app.
  if (!session) {
    return <SignInScreen />;
  }

  return <Outlet />;
}
