import type { HealthState } from "../hooks/useHealth";
import { Badge } from "./ui/Badge";
import styles from "./HealthBadge.module.css";

const DEPENDENCY_LABEL: Record<string, string> = {
  cache: "캐시",
  vector_store: "벡터 스토어",
};

export function HealthBadge({ state }: { state: HealthState }) {
  if (state.status === "unknown") {
    return (
      <div className={styles.wrap}>
        <Badge tone="warn" dot>
          상태 알 수 없음
        </Badge>
      </div>
    );
  }

  const { report } = state;
  const healthy = report.status === "ok";
  return (
    <div className={styles.wrap}>
      <Badge tone={healthy ? "ok" : "danger"} dot>
        {healthy ? "정상" : "의존성 불능"}
      </Badge>
      <ul className={styles.list}>
        {Object.entries(report.dependencies).map(([name, dependency]) => (
          <li key={name}>
            <Badge tone={dependency.status === "ok" ? "ok" : "danger"} dot>
              {DEPENDENCY_LABEL[name] ?? name} {dependency.status === "ok" ? "정상" : "불능"}
            </Badge>
            {dependency.detail ? <span className={styles.detail}>{dependency.detail}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
