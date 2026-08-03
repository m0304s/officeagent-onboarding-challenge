import type { HealthState } from "../hooks/useHealth";
import { HealthBadge } from "./HealthBadge";
import styles from "./AppHeader.module.css";

export function AppHeader({ health }: { health: HealthState }) {
  return (
    <header className={styles.header}>
      <div className={styles.titles}>
        <h1 className={styles.title}>문서 Q&amp;A 데모 콘솔</h1>
        <p className={styles.subtitle}>
          업로드한 문서만을 근거로 답변합니다. 답변은 조각 단위로 도착하며 각 문장의 근거를 함께 표시합니다.
        </p>
      </div>
      <HealthBadge state={health} />
    </header>
  );
}
