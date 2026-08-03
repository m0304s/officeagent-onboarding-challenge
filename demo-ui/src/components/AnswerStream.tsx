import { useMemo } from "react";

import type { DonePayload } from "../api/types";
import type { QaPhase } from "../hooks/useQaStream";
import { formatElapsed } from "../lib/format";
import { AnswerText } from "./AnswerText";
import { CitationList } from "./CitationList";
import { Badge } from "./ui/Badge";
import styles from "./AnswerStream.module.css";

export function AnswerStream({
  phase,
  text,
  result,
}: {
  phase: QaPhase;
  text: string;
  result: DonePayload | null;
}) {
  // 스트리밍 중에는 검증된 마커가 아직 없다. 빈 집합이면 본문의 `[n]` 이 전부 평문으로
  // 남는데, 그게 맞다 — 검증 전 마커를 링크로 만들면 근거인 척하게 된다.
  const markers = useMemo(
    () => new Set((result?.citations ?? []).map((citation) => citation.marker)),
    [result],
  );

  const streaming = phase === "streaming" || phase === "preparing";
  if (text === "" && !streaming) return null;

  return (
    <section
      className={styles.wrap}
      role="region"
      aria-labelledby="answer-heading"
      aria-busy={streaming || undefined}
    >
      <div className={styles.head}>
        <h3 id="answer-heading" className={styles.heading}>
          답변
        </h3>
        {result ? (
          <div className={styles.stats}>
            <Badge tone="neutral">{formatElapsed(result.elapsed_ms)}</Badge>
            {result.dropped_markers > 0 ? (
              <Badge tone="warn">검증되지 않은 마커 {result.dropped_markers}개</Badge>
            ) : null}
          </div>
        ) : null}
      </div>

      <p className={styles.body}>
        <AnswerText text={text} markers={markers} />
        {streaming ? <span className={styles.caret} aria-hidden="true" /> : null}
      </p>

      {result ? <CitationList citations={result.citations} /> : null}
    </section>
  );
}
