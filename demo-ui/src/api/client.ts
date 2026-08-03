// HTTP 경계. 오류 봉투를 `AppError` 하나로 모아 화면이 상태 코드를 직접 보지 않게 한다.

import type { DocumentListView, HealthReport, UploadView } from "./types";

const BASE = "/api";

/** 서버가 닿지 않아 봉투 자체가 없는 경우. 서버의 `ErrorCode` 와 겹치지 않는 이름이다. */
export const NETWORK_UNREACHABLE = "network_unreachable";

export class AppError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly extra: Record<string, unknown>;

  constructor(code: string, message: string, status: number | null, extra: Record<string, unknown> = {}) {
    super(message);
    this.name = "AppError";
    this.code = code;
    this.status = status;
    this.extra = extra;
  }

  /** 상한 초과처럼 값이 함께 오는 오류에서 숫자 하나를 꺼낸다. */
  number(key: string): number | null {
    const value = this.extra[key];
    return typeof value === "number" ? value : null;
  }

  strings(key: string): string[] {
    const value = this.extra[key];
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  }
}

async function toAppError(response: Response): Promise<AppError> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // 본문이 JSON 이 아닌 실패(프록시가 만든 502 등)도 같은 타입으로 나가야 한다.
  }
  const envelope =
    payload && typeof payload === "object" && "error" in payload
      ? (payload as { error: Record<string, unknown> }).error
      : null;

  if (!envelope) {
    return new AppError("internal_error", `요청이 ${response.status} 로 실패했습니다`, response.status);
  }
  const { code, message, ...extra } = envelope;
  return new AppError(
    typeof code === "string" ? code : "internal_error",
    typeof message === "string" ? message : "요청을 처리할 수 없습니다",
    response.status,
    extra,
  );
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, init);
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new AppError(NETWORK_UNREACHABLE, "API 서버에 닿지 못했습니다", null);
  }
  if (!response.ok) throw await toAppError(response);
  return response;
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthReport> {
  // 200 과 503 이 본문 모양이 같다. 상태 코드로 갈라 예외를 던지면 어느 의존성이
  // 죽었는지가 사라지므로 `request` 를 거치지 않는다.
  let response: Response;
  try {
    response = await fetch(`${BASE}/health`, { signal });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new AppError(NETWORK_UNREACHABLE, "API 서버에 닿지 못했습니다", null);
  }
  return (await response.json()) as HealthReport;
}

export async function listDocuments(signal?: AbortSignal): Promise<DocumentListView> {
  const response = await request("/documents", { signal });
  return (await response.json()) as DocumentListView;
}

export async function uploadDocument(file: File, signal?: AbortSignal): Promise<UploadView> {
  const form = new FormData();
  form.append("file", file);
  // `Content-Type` 을 직접 넣지 않는다 — 넣으면 multipart 경계 문자열이 빠져 파싱이 깨진다.
  const response = await request("/documents", { method: "POST", body: form, signal });
  return (await response.json()) as UploadView;
}

export async function deleteDocument(documentId: string, signal?: AbortSignal): Promise<void> {
  await request(`/documents/${encodeURIComponent(documentId)}`, { method: "DELETE", signal });
}

/** `/qa` 응답 본문을 연다. 스트림 밖 실패는 여기서 `AppError` 로 끝난다. */
export async function openQaStream(
  question: string,
  topK: number | null,
  signal: AbortSignal,
): Promise<ReadableStream<Uint8Array>> {
  const response = await request("/qa", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(topK === null ? { question } : { question, top_k: topK }),
    signal,
  });
  if (!response.body) {
    throw new AppError("internal_error", "응답 본문이 비어 있습니다", response.status);
  }
  return response.body;
}
