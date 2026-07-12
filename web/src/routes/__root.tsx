import { createRootRoute, Outlet } from "@tanstack/react-router";
import { useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { SignInScreen } from "../components/SignInScreen";
import { useAuth } from "../lib/auth";
import { listFamilies, bootstrapFamily } from "../api/sdk.gen";
import styles from "./__root.module.css";

export const Route = createRootRoute({
  component: RootComponent,
});

function RootComponent() {
  const { loading, session, user } = useAuth();

  if (loading) {
    return (
      <div className={styles.loadingShell} data-mode="parent">
        Loading…
      </div>
    );
  }

  if (!session) {
    return <SignInScreen />;
  }

  return (
    <div data-mode="parent" className={styles.appRoot}>
      <BootstrapGate userId={session.user.id} displayName={user?.user_metadata?.full_name ?? user?.email ?? ""}>
        <Outlet />
      </BootstrapGate>
    </div>
  );
}

/**
 * Silently bootstraps the family on first authed load.
 * If listFamilies returns an empty array, calls bootstrapFamily once, then
 * re-queries. Children render only after the family is confirmed to exist.
 */
function BootstrapGate({
  children,
  displayName,
}: {
  userId: string;
  displayName: string;
  children: React.ReactNode;
}) {
  const qc = useQueryClient();

  const { data: families, isLoading: familiesLoading } = useQuery({
    queryKey: ["families"],
    queryFn: async () => {
      const res = await listFamilies();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  const bootstrap = useMutation({
    mutationFn: async (familyName: string) => {
      const res = await bootstrapFamily({ body: { family_name: familyName } });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["families"] });
    },
  });

  // Fire bootstrap exactly once when families list resolves to empty
  useEffect(() => {
    if (!familiesLoading && families && families.length === 0 && !bootstrap.isPending && !bootstrap.isSuccess) {
      // Derive family name from the Google display name or email
      const firstName = displayName.split(/[\s@]/)[0] ?? "My";
      const familyName = `${firstName}'s family`;
      bootstrap.mutate(familyName);
    }
  }, [familiesLoading, families, bootstrap, displayName]);

  if (familiesLoading || bootstrap.isPending) {
    return (
      <div className={styles.loadingShell}>
        Setting up your family…
      </div>
    );
  }

  return <>{children}</>;
}
