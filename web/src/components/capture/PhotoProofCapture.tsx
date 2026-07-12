import { useState } from "react";
import styles from "./PhotoProofCapture.module.css";

interface PhotoProofCaptureProps {
  /** Called with the local preview URL (not uploaded — Phase 1 deferred). */
  onPhotoSelected?: (localUrl: string) => void;
  onSkip: () => void;
}

/**
 * Optional, skippable photo-proof capture step.
 * Does NOT upload to Storage (deferred to Phase 2 wiring).
 * Renders a file input with camera capture for mobile devices.
 * Submission always uses proof_photo_paths: [].
 */
export function PhotoProofCapture({ onPhotoSelected, onSkip }: PhotoProofCaptureProps) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    onPhotoSelected?.(url);
  }

  return (
    <div className={styles.wrapper}>
      <h2 className={styles.heading}>Take a photo of your paper</h2>
      <p className={styles.note}>
        This is optional — it's just a record so your parent can see your working. You can skip it.
      </p>

      {previewUrl ? (
        <>
          <div className={styles.preview}>
            <img src={previewUrl} alt="Photo of your paper" className={styles.previewImg} />
          </div>
          <label className={styles.label}>
            <span className={styles.cameraIcon} aria-hidden="true">📷</span>
            <span className={styles.labelText}>Take another photo</span>
            <input
              type="file"
              accept="image/*"
              capture="environment"
              className={styles.hiddenInput}
              onChange={handleFileChange}
            />
          </label>
        </>
      ) : (
        <label className={styles.label}>
          <span className={styles.cameraIcon} aria-hidden="true">📷</span>
          <span className={styles.labelText}>Tap to take a photo</span>
          <input
            type="file"
            accept="image/*"
            capture="environment"
            className={styles.hiddenInput}
            onChange={handleFileChange}
          />
        </label>
      )}

      <button type="button" className={styles.skipBtn} onClick={onSkip}>
        Skip photo
      </button>
    </div>
  );
}
