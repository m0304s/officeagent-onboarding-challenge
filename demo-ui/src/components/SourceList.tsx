import { useState } from "react";

import type { SourcesPayload } from "../api/types";
import { formatContributions, formatLocation, formatScore } from "../lib/format";
import { Badge } from "./ui/Badge";
import styles from "./SourceList.module.css";

const PREVIEW_CHARS = 220;

export function SourceList({ sources }: { sources: SourcesPayload }) {
  return (
    <section className={styles.wrap} aria-labelledby="sources-heading">
      <div className={styles.head}>
        <h3 id="sources-heading" className={styles.heading}>
          검색된 근거 {sources.count}건
        </h3>
        <Badge tone="neutral">
          상위 {sources.top_k} · 대상 문서 {sources.target_documents}건
        </Badge>
      </div>
      <ul className={styles.list}>
        {sources.results.map((chunk) => (
          <SourceItem key={`${chunk.document_id}-${chunk.chunk_index}`} chunk={chunk} />
        ))}
      </ul>
    </section>
  );
}

function SourceItem({ chunk }: { chunk: SourcesPayload["results"][number] }) {
  const [expanded, setExpanded] = useState(false);
  const long = chunk.text.length > PREVIEW_CHARS;
  const shown = expanded || !long ? chunk.text : `${chunk.text.slice(0, PREVIEW_CHARS)}…`;

  return (
    <li className={styles.item}>
      <div className={styles.itemHead}>
        <span className={styles.filename} title={chunk.filename}>
          {chunk.filename}
        </span>
        <span
          className={styles.score}
          title="활성 retriever 들이 이 청크를 얼마나 나란히 상위로 꼽았는가. 유사도가 아니다"
        >
          융합 점수 {formatScore(chunk.score)}
        </span>
      </div>
      <p className={styles.text}>{shown}</p>
      <div className={styles.itemFoot}>
        <span className={styles.meta}>
          {formatLocation(chunk)}
          {chunk.contributions.length > 0 ? ` · ${formatContributions(chunk.contributions)}` : ""}
        </span>
        {long ? (
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "접기" : "전문 펼치기"}
          </button>
        ) : null}
      </div>
    </li>
  );
}
