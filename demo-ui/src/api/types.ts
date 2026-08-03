// 서버 응답 스키마를 손으로 옮긴 것. 생성기를 붙이지 않은 이유는 백엔드 기동 없이
// 프런트가 빌드되지 않게 되기 때문이다. 대조 근거는 `src/app/api/` 의 Pydantic 모델.

export type DocumentFormat = "txt" | "md" | "pdf";
export type IndexStatus = "indexed" | "stale";
export type IngestionStatus = "created" | "replaced" | "reindexed" | "unchanged";
export type FinishReason = "stop" | "no_evidence" | "insufficient_evidence";
export type FailureReason = "timeout" | "generation_failed" | "unauthenticated";
export type DependencyStatus = "ok" | "unavailable";

export interface DocumentView {
  document_id: string;
  filename: string;
  format: DocumentFormat;
  revision: string;
  index_signature: string;
  index_status: IndexStatus;
  chunk_count: number;
  byte_size: number;
  ingested_at: string;
}

export interface UploadView extends DocumentView {
  status: IngestionStatus;
  previous_revision: string | null;
}

export interface DocumentListView {
  documents: DocumentView[];
  count: number;
}

/** 근거 청크 하나. `/search` 결과와 SSE `sources` 항목이 같은 모양이다. */
export interface SearchResultView {
  document_id: string;
  filename: string;
  format: DocumentFormat;
  revision: string;
  chunk_index: number;
  text: string;
  score: number;
  char_start: number;
  char_end: number;
  page: number | null;
}

/** 답변이 실제로 인용한 근거. `text` 가 없고 `marker` 가 있다. */
export interface CitationView {
  marker: number;
  document_id: string;
  filename: string;
  format: DocumentFormat;
  revision: string;
  chunk_index: number;
  score: number;
  char_start: number;
  char_end: number;
  page: number | null;
}

export interface SourcesPayload {
  results: SearchResultView[];
  count: number;
  top_k: number;
  /** 지금 검색 대상인 문서 수. 0 이면 수집된 문서가 없다는 뜻이다. */
  target_documents: number;
}

export interface AnswerPayload {
  text: string;
}

export interface DonePayload {
  finish_reason: FinishReason;
  answer: string;
  citations: CitationView[];
  dropped_markers: number;
  elapsed_ms: number;
}

export interface ErrorPayload {
  code: string;
  message: string;
  attempts: number;
  reason: FailureReason;
}

export interface HealthDependency {
  status: DependencyStatus;
  detail: string | null;
}

/** `dependencies` 는 배열이 아니라 이름을 키로 하는 객체다. */
export interface HealthReport {
  status: DependencyStatus;
  dependencies: Record<string, HealthDependency>;
}
