import type { CitationView, ContributionView, SearchResultView } from "../api/types";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatScore(score: number): string {
  return score.toFixed(3);
}

/**
 * 어느 retriever 가 몇 위로 올렸는지. 원점수는 척도가 서로 달라 싣지 않는다.
 *
 * 이름 하나가 빠진 것이 하이브리드가 꺼졌다는 유일한 신호라, 이 줄이 데모에서 그 신호를
 * 보는 자리다.
 */
export function formatContributions(contributions: ContributionView[]): string {
  return contributions.map((item) => `${item.retriever} ${item.rank}위`).join(" · ");
}

/** 좁은 패널에 들어가야 해서 연도를 뺀다 — 데모에서 구분에 필요한 것은 월·일·시각이다. */
export function formatDateTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 청크가 원문의 어디였는지. PDF 는 쪽 번호가 있고 텍스트는 문자 범위뿐이다. */
export function formatLocation(chunk: SearchResultView | CitationView): string {
  const range = `${chunk.char_start.toLocaleString()}–${chunk.char_end.toLocaleString()}자`;
  return chunk.page === null ? `청크 ${chunk.chunk_index} · ${range}` : `${chunk.page}쪽 · 청크 ${chunk.chunk_index} · ${range}`;
}

export function formatElapsed(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}초`;
}
