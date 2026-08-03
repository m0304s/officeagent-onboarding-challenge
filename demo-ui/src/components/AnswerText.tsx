import type { ReactNode } from "react";

import styles from "./AnswerText.module.css";

const MARKER = /\[(\d+)\]/g;

/**
 * 본문의 `[n]` 중 **서버가 검증한 번호만** 근거 링크로 만든다. 검증되지 않은 마커를
 * 지우지 않는 이유는, 지우면 흘러간 문장과 최종 문장이 달라지기 때문이다.
 */
export function AnswerText({ text, markers }: { text: string; markers: ReadonlySet<number> }) {
  if (markers.size === 0) return <>{text}</>;

  const parts: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  MARKER.lastIndex = 0;

  while ((match = MARKER.exec(text)) !== null) {
    const marker = Number(match[1]);
    if (!markers.has(marker)) continue;
    parts.push(text.slice(cursor, match.index));
    parts.push(
      <a key={`${match.index}-${marker}`} className={styles.marker} href={`#citation-${marker}`}>
        [{marker}]
      </a>,
    );
    cursor = match.index + match[0].length;
  }
  parts.push(text.slice(cursor));

  return <>{parts}</>;
}
