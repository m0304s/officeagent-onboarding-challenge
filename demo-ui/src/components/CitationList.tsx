import type { CitationDetail } from "../hooks/useQaStream";
import { formatLocation, formatScore } from "../lib/format";
import styles from "./CitationList.module.css";

export function CitationList({
  citations,
  expanded,
  onToggle,
}: {
  citations: CitationDetail[];
  expanded: ReadonlySet<number>;
  onToggle: (marker: number) => void;
}) {
  if (citations.length === 0) return null;

  return (
    <section className={styles.wrap} aria-labelledby="citations-heading">
      <h3 id="citations-heading" className={styles.heading}>
        인용한 근거 {citations.length}건
      </h3>
      <ol className={styles.list}>
        {citations.map((citation) => (
          <CitationItem
            key={citation.marker}
            citation={citation}
            open={expanded.has(citation.marker)}
            onToggle={onToggle}
          />
        ))}
      </ol>
    </section>
  );
}

function CitationItem({
  citation,
  open,
  onToggle,
}: {
  citation: CitationDetail;
  open: boolean;
  onToggle: (marker: number) => void;
}) {
  const bodyId = `citation-body-${citation.marker}`;
  const summary = (
    <>
      <span className={styles.marker} aria-hidden="true">
        [{citation.marker}]
      </span>
      <span className={styles.body}>
        <span className={styles.filename}>{citation.filename}</span>
        <span className={styles.meta}>
          {formatLocation(citation)} · 융합 점수 {formatScore(citation.score)}
        </span>
      </span>
    </>
  );

  return (
    // `tabIndex` 가 없으면 마커에서 온 앵커 이동이 초점을 옮기지 못해, 스크린 리더
    // 사용자에게는 아무 일도 일어나지 않은 것이 된다.
    <li id={`citation-${citation.marker}`} className={styles.item} tabIndex={-1}>
      {citation.text === null ? (
        // 본문을 못 찾았으면 펼침 어포던스를 아예 주지 않는다. 눌러도 빈 칸이 열리는
        // 버튼은 고장으로 읽힌다.
        <div className={styles.summary}>{summary}</div>
      ) : (
        <button type="button" className={styles.trigger} aria-expanded={open} aria-controls={bodyId} onClick={() => onToggle(citation.marker)}>
          <span className={styles.chevron} data-open={open || undefined} aria-hidden="true" />
          {summary}
          <span className="srOnly">{open ? "근거 본문 접기" : "근거 본문 펼치기"}</span>
        </button>
      )}
      {/* 접혔을 때도 DOM 에 남긴다. 빼 버리면 `aria-controls` 가 없는 id 를 가리킨다. */}
      {citation.text !== null ? (
        <p id={bodyId} className={styles.text} hidden={!open}>
          {citation.text}
        </p>
      ) : null}
    </li>
  );
}
