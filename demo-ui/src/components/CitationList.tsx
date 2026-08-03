import type { CitationView } from "../api/types";
import { formatLocation, formatScore } from "../lib/format";
import styles from "./CitationList.module.css";

export function CitationList({ citations }: { citations: CitationView[] }) {
  if (citations.length === 0) return null;

  return (
    <section className={styles.wrap} aria-labelledby="citations-heading">
      <h3 id="citations-heading" className={styles.heading}>
        인용한 근거 {citations.length}건
      </h3>
      <ol className={styles.list}>
        {citations.map((citation) => (
          // `tabIndex` 가 없으면 마커에서 온 앵커 이동이 초점을 옮기지 못해, 스크린 리더
          // 사용자에게는 아무 일도 일어나지 않은 것이 된다.
          <li key={citation.marker} id={`citation-${citation.marker}`} className={styles.item} tabIndex={-1}>
            <span className={styles.marker} aria-hidden="true">
              [{citation.marker}]
            </span>
            <div className={styles.body}>
              <span className={styles.filename}>{citation.filename}</span>
              <span className={styles.meta}>
                {formatLocation(citation)} · 유사도 {formatScore(citation.score)}
              </span>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
