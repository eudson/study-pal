import { StickerButton } from "../StickerButton";
import styles from "./SubmitCelebration.module.css";

interface SubmitCelebrationProps {
  childName: string;
  onDone: () => void;
}

/**
 * Celebratory full-screen shown after successful submission.
 * Teacher-stamp aesthetic: circle, rotated, teal family.
 * uiux agent will layer confetti / additional motion here.
 */
export function SubmitCelebration({ childName, onDone }: SubmitCelebrationProps) {
  return (
    <div className={styles.shell} data-mode="child">
      {/* Gold stars — DESIGN §5: celebration with gold stars */}
      <div className={styles.stars} aria-hidden="true">
        <span className={styles.starLeft}>★</span>
        <span className={styles.starCenter}>★</span>
        <span className={styles.starRight}>★</span>
      </div>

      {/* Teacher stamp — DESIGN §4: circle, 3px teal border, rotate(-8deg), Fredoka caption */}
      <div className={styles.stamp} aria-hidden="true">
        {/* stamp text: no "!" — the one allowed "!" is on the headline — DESIGN §7 */}
        <span className={styles.stampText}>Done</span>
      </div>

      <h1 className={styles.headline}>
        You did it{childName ? `, ${childName}` : ""}!
      </h1>

      <p className={styles.subtext}>
        Your answers have been saved. Your parent will take a look soon.
      </p>

      <StickerButton className={styles.doneBtn} onClick={onDone}>
        Back to home
      </StickerButton>
    </div>
  );
}
