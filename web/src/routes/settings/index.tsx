import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { listChildren, listSubjects } from "../../api/sdk.gen";
import { useAuth } from "../../lib/auth";
import styles from "./-settings.module.css";

export const Route = createFileRoute("/settings/")({
  component: SettingsPage,
});

function SettingsPage() {
  const navigate = useNavigate();
  const { user, signOut } = useAuth();

  const { data: children = [], isLoading } = useQuery({
    queryKey: ["children"],
    queryFn: async () => {
      const res = await listChildren();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  const { data: subjects = [] } = useQuery({
    queryKey: ["subjects"],
    queryFn: async () => {
      const res = await listSubjects();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  // Group subjects by child_id
  const subjectsByChild = subjects.reduce<Record<string, string[]>>(
    (acc, s) => {
      const arr = acc[s.child_id] ?? [];
      arr.push(s.name);
      acc[s.child_id] = arr;
      return acc;
    },
    {},
  );

  const fullName = (user?.user_metadata?.full_name as string | undefined) ?? "";
  const email = user?.email ?? "";

  if (isLoading) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} />
      </div>
    );
  }

  return (
    <div className={styles.shell}>
      {/* Header */}
      <div className={styles.header}>
        <button
          type="button"
          className={styles.backBtn}
          aria-label="Back"
          onClick={() => void navigate({ to: "/" })}
        >
          ‹
        </button>
        <h1 className={styles.pageTitle}>Settings</h1>
      </div>

      {/* Family section */}
      <section>
        <div className={styles.sectionLabel}>Family</div>
        <div className={styles.childList}>
          {children.map((child) => (
            <button
              key={child.id}
              type="button"
              className={styles.childCard}
              onClick={() =>
                void navigate({
                  to: "/settings/children/$childId",
                  params: { childId: child.id },
                })
              }
            >
              <div className={styles.childCardRow}>
                <span className={styles.childName}>{child.display_name}</span>
                <span className={styles.childGrade}>{child.grade_label} ›</span>
              </div>
              {(subjectsByChild[child.id] ?? []).length > 0 && (
                <div className={styles.chipRow}>
                  {(subjectsByChild[child.id] ?? []).map((name) => (
                    <span key={name} className={styles.subjectChip}>
                      {name}
                    </span>
                  ))}
                </div>
              )}
            </button>
          ))}

          <button
            type="button"
            className={styles.addChildBtn}
            onClick={() => void navigate({ to: "/settings/children/new" })}
          >
            + Add child
          </button>
        </div>
      </section>

      {/* Account section */}
      <section>
        <div className={styles.sectionLabel}>Account</div>
        <div className={styles.accountCard}>
          {fullName ? (
            <div className={styles.accountRow}>
              <span className={styles.accountLabel}>Name</span>
              <span className={styles.accountValue}>{fullName}</span>
            </div>
          ) : null}
          <div className={styles.accountRow}>
            <span className={styles.accountLabel}>Google</span>
            <span className={styles.accountValue}>{email}</span>
          </div>
          <button
            type="button"
            className={styles.signOutBtn}
            onClick={() => void signOut()}
          >
            Sign out
          </button>
        </div>
      </section>

      {/* Defaults section — informational; taps into child list */}
      <section>
        <div className={styles.sectionLabel}>Defaults</div>
        <button
          type="button"
          className={styles.defaultsCard}
          onClick={() => void navigate({ to: "/settings" })}
        >
          <span className={styles.defaultsLabel}>Visibility defaults</span>
          <span className={styles.defaultsMeta}>per child ›</span>
        </button>
      </section>
    </div>
  );
}
