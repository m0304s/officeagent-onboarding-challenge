import type { ReactNode } from "react";

import styles from "./Badge.module.css";

export type BadgeTone = "neutral" | "ok" | "warn" | "danger" | "info";

const TONE_CLASS: Record<BadgeTone, string | undefined> = {
  neutral: undefined,
  ok: styles.ok,
  warn: styles.warn,
  danger: styles.danger,
  info: styles.info,
};

export function Badge({
  tone = "neutral",
  dot = false,
  children,
}: {
  tone?: BadgeTone;
  dot?: boolean;
  children: ReactNode;
}) {
  return (
    <span className={[styles.badge, TONE_CLASS[tone]].filter(Boolean).join(" ")}>
      {dot ? <span className={styles.dot} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}
