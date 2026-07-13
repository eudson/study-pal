import { useState } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { getStudyPack, generateStudyPack, approveStudyPack } from "../../api/sdk.gen";
import { StudyPackCard } from "../../components/StudyPackCard";
import { StickerButton } from "../../components/StickerButton";
import { Chip } from "../../components/Chip";
import { supabase } from "../../lib/supabase";
import styles from "./-study-pack.module.css";

export const Route = createFileRoute("/cycles/$cycleId/study-pack")({
  component: StudyPackPage,
});

// ─── PDF download helper ──────────────────────────────────────────────────────
//
// The generated SDK types GetStudyPackPdfResponses as `unknown` (binary blob).
// Rather than fighting the generic client for binary data, we issue a direct
// fetch using the same auth credentials (Bearer + X-User-Id) and trigger a
// browser download from the resulting blob.

async function downloadStudyPackPdf(cycleId: string): Promise<void> {
  const { data: sessionData } = await supabase.auth.getSession();
  const token = sessionData.session?.access_token;
  const userId = sessionData.session?.user?.id;

  const baseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
  const url = `${baseUrl}/cycles/${cycleId}/study-pack/pdf`;

  const headers: Record<string, string> = {
    Accept: "application/pdf",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (userId) headers["X-User-Id"] = userId;

  const response = await fetch(url, { headers });
  if (!response.ok) {
    throw new Error(`PDF download failed (${response.status.toString()})`);
  }

  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);

  // Trigger download via temporary anchor
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = `study-pack-${cycleId}.pdf`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);

  // Release the object URL after a short delay so the download can start
  setTimeout(() => URL.revokeObjectURL(objectUrl), 5000);
}

// ─── StudyPackPage ────────────────────────────────────────────────────────────

function StudyPackPage() {
  const { cycleId } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [pdfError, setPdfError] = useState<string | null>(null);
  const [pdfPending, setPdfPending] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);

  // ── Load study pack; auto-generate on 404 ──
  const {
    data: packResponse,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["study-pack", cycleId],
    queryFn: async () => {
      const res = await getStudyPack({ path: { cycle_id: cycleId } });

      // 404 → pack not yet generated; fire generate exactly once (idempotent).
      // The HTTP status lives on res.response, not on the error body (which is
      // FastAPI's { detail } shape).
      if (res.error) {
        if (res.response?.status === 404) {
          const generated = await generateStudyPack({ path: { cycle_id: cycleId } });
          if (generated.error) throw generated.error;
          if (!generated.data) throw new Error("Study pack generation failed");
          return generated.data;
        }
        throw res.error;
      }

      if (!res.data) throw new Error("No study pack data");
      return res.data;
    },
    // No automatic retry — the generate-if-missing path above handles the
    // single attempt. Retries would loop the generate call unnecessarily.
    retry: false,
  });

  // ── Approve mutation (golden rule 8 gate) ──
  const approveMutation = useMutation({
    mutationFn: async () => {
      setApproveError(null);
      const res = await approveStudyPack({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Approval failed");
      return res.data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["study-pack", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
    },
    onError: (err: unknown) => {
      setApproveError(err instanceof Error ? err.message : "Approval failed");
    },
  });

  // ── PDF download handler ──
  async function handleDownloadPdf() {
    setPdfError(null);
    setPdfPending(true);
    try {
      await downloadStudyPackPdf(cycleId);
    } catch (err) {
      setPdfError(err instanceof Error ? err.message : "PDF download failed");
    } finally {
      setPdfPending(false);
    }
  }

  // ── Loading / generating ──
  if (isLoading) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} aria-hidden="true" />
        <p className={styles.generatingLabel}>Building the study pack…</p>
      </div>
    );
  }

  // ── Hard error ──
  if (error) {
    const msg =
      error instanceof Error ? error.message : "Failed to load the study pack";
    return (
      <div className={styles.shell}>
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <button
              type="button"
              className={styles.backBtn}
              aria-label="Back"
              onClick={() =>
                void navigate({ to: "/cycles/$cycleId", params: { cycleId } })
              }
            >
              ‹
            </button>
            <div className={styles.pageTitle}>Study pack</div>
          </div>
        </div>
        <div className={styles.errorBox} role="alert">
          {msg}
        </div>
      </div>
    );
  }

  if (!packResponse) return null;

  const { pack, approved_at } = packResponse;
  const items = pack.items ?? [];
  const gapTags = pack.derived_from_gap_tags ?? [];
  const isApproved = Boolean(approved_at);

  // Readable timestamp for confirmed approval (factual, no exclamation — DESIGN §7)
  const approvedTimestamp = approved_at
    ? new Date(approved_at).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : null;

  return (
    <div className={styles.shell}>
      {/* ── Header ── */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label="Back to cycle"
            onClick={() =>
              void navigate({ to: "/cycles/$cycleId", params: { cycleId } })
            }
          >
            ‹
          </button>
          <div className={styles.headerText}>
            <div className={styles.pageTitle}>Study pack</div>
            {/* Factual subtitle — item count + growing areas, never game chrome. */}
            <div
              className={styles.subtitleLine}
              aria-label={`${items.length.toString()} practice items targeting ${gapTags.length.toString()} growing areas`}
            >
              <span>{items.length} practice {items.length === 1 ? "item" : "items"}</span>
              {gapTags.length > 0 && (
                <>
                  <span aria-hidden="true">·</span>
                  <span className={styles.subtitleGrowing}>
                    {gapTags.length} growing {gapTags.length === 1 ? "area" : "areas"}
                  </span>
                </>
              )}
            </div>
          </div>
        </div>
        {/* Approved chip — teal = confirmed (DESIGN §2). Only shown once approved. */}
        {isApproved && <Chip variant="teal">Approved</Chip>}
      </div>

      {/* ── Approved confirmation banner ── */}
      {isApproved && approvedTimestamp && (
        <div className={styles.approvedRow} role="status">
          <span className={styles.approvedText}>
            Approved for your child on {approvedTimestamp}. The pack is now visible to them.
          </span>
        </div>
      )}

      {/* ── Pack summary / intro paragraph ── */}
      {pack.summary && (
        <p className={styles.summaryText}>{pack.summary}</p>
      )}

      {/* ── Scrollable list of practice items ── */}
      <div className={styles.list}>
        {items.length === 0 ? (
          <div className={styles.emptyState}>
            No practice items in this pack.
          </div>
        ) : (
          items.map((item, i) => (
            <StudyPackCard key={item.item_id} item={item} index={i} />
          ))
        )}
      </div>

      {/* ── Footer actions ── */}
      <div className={styles.footer}>
        {/* Error states */}
        {approveError && (
          <div className={styles.errorBox} role="alert">
            {approveError}
          </div>
        )}
        {pdfError && (
          <div className={styles.errorBox} role="alert">
            {pdfError}
          </div>
        )}

        {/* Approval gate (golden rule 8 — recorded parent approval before child visibility).
            Once approved, this is replaced by the confirmation banner above. */}
        {!isApproved && (
          <StickerButton
            className={styles.ctaFull}
            disabled={approveMutation.isPending}
            onClick={() => approveMutation.mutate()}
          >
            {approveMutation.isPending ? "Approving…" : "Approve for my child"}
          </StickerButton>
        )}

        {/* Download PDF — secondary, always available once pack exists */}
        <button
          type="button"
          className={styles.secondaryBtn}
          disabled={pdfPending}
          onClick={() => void handleDownloadPdf()}
        >
          {pdfPending ? "Preparing PDF…" : "Download printable PDF"}
        </button>
      </div>
    </div>
  );
}
