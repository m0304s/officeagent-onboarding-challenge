import { useCallback, useEffect, useMemo, useState } from "react";

import type { DonePayload } from "../api/types";
import type { CitationDetail, QaPhase } from "../hooks/useQaStream";
import { formatElapsed } from "../lib/format";
import { AnswerText } from "./AnswerText";
import { CitationList } from "./CitationList";
import { Badge } from "./ui/Badge";
import styles from "./AnswerStream.module.css";

export function AnswerStream({
  phase,
  text,
  result,
  citations,
}: {
  phase: QaPhase;
  text: string;
  result: DonePayload | null;
  citations: CitationDetail[];
}) {
  // 본문의 마커와 인용 목록이 같은 집합을 보고 열려야 해서, 상태가 둘의 조상에 있다.
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(() => new Set());

  // 새 답변은 접힌 채로 시작한다. 안 비우면 앞 답변에서 열어 둔 번호가 그대로 열려 보인다.
  useEffect(() => setExpanded(new Set()), [result]);

  const toggle = useCallback((marker: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(marker)) next.add(marker);
      return next;
    });
  }, []);

  // 마커는 토글이 아니라 열기다 — 링크를 눌렀는데 닫히면 이동만 하고 사라진 것이 된다.
  const open = useCallback((marker: number) => {
    setExpanded((current) => (current.has(marker) ? current : new Set(current).add(marker)));
  }, []);

  // 스트리밍 중에는 검증된 마커가 아직 없다. 빈 집합이면 본문의 `[n]` 이 전부 평문으로
  // 남는데, 그게 맞다 — 검증 전 마커를 링크로 만들면 근거인 척하게 된다.
  const markers = useMemo(() => new Set(citations.map((citation) => citation.marker)), [citations]);

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
        <AnswerText text={text} markers={markers} onSelect={open} />
        {streaming ? <span className={styles.caret} aria-hidden="true" /> : null}
      </p>

      <CitationList citations={citations} expanded={expanded} onToggle={toggle} />
    </section>
  );
}
