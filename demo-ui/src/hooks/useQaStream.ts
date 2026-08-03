// 질문 하나의 수명. 상태 기계가 한 곳에 있어야 "조각이 섞였다" 류의 사고가 안 난다.

import { useCallback, useEffect, useMemo, useRef, useReducer } from "react";

import { AppError, NETWORK_UNREACHABLE, openQaStream } from "../api/client";
import { readSseFrames } from "../api/sse";
import type { AnswerPayload, DonePayload, ErrorPayload, SourcesPayload } from "../api/types";

export type QaPhase = "idle" | "preparing" | "streaming" | "answered" | "rejected" | "failed" | "aborted";

/** 화면이 서로 다른 문구를 써야 하는 실패들. 서버 코드 하나에 사유 둘이 실려 오는 경우가 있다. */
export type QaFailure =
  | { kind: "validation"; message: string; maxQueryChars: number | null; maxQueryTokens: number | null; maxTopK: number | null }
  | { kind: "unauthenticated"; message: string }
  | { kind: "generation"; message: string; attempts: number; reason: "timeout" | "generation_failed" }
  | { kind: "network"; message: string }
  | { kind: "disconnected"; message: string }
  | { kind: "other"; code: string; message: string };

/** 거절 세 갈래. 문서가 없는 것과 근거를 못 찾은 것은 사용자가 할 일이 다르다. */
export type QaRejection = "no_documents" | "no_relevant_evidence" | "insufficient_evidence";

export interface QaState {
  phase: QaPhase;
  question: string;
  sources: SourcesPayload | null;
  chunks: string[];
  result: DonePayload | null;
  failure: QaFailure | null;
}

type QaAction =
  | { type: "start"; question: string }
  | { type: "sources"; payload: SourcesPayload }
  | { type: "answer"; text: string }
  | { type: "done"; payload: DonePayload }
  | { type: "failed"; failure: QaFailure }
  | { type: "aborted" }
  | { type: "reset" };

const INITIAL: QaState = {
  phase: "idle",
  question: "",
  sources: null,
  chunks: [],
  result: null,
  failure: null,
};

function reduce(state: QaState, action: QaAction): QaState {
  switch (action.type) {
    case "start":
      // 통째로 갈아끼운다. 앞선 답변의 조각이 새 질문에 섞이는 사고는 전부 여기서 막힌다.
      return { ...INITIAL, phase: "preparing", question: action.question };
    case "sources":
      return { ...state, phase: "streaming", sources: action.payload };
    case "answer":
      // 문자열을 누적하면 조각마다 O(n) 복사가 쌓인다. 합치는 것은 렌더가 한다.
      return { ...state, chunks: [...state.chunks, action.text] };
    case "done":
      return {
        ...state,
        phase: action.payload.finish_reason === "stop" ? "answered" : "rejected",
        result: action.payload,
      };
    case "failed":
      return { ...state, phase: "failed", failure: action.failure };
    case "aborted":
      return { ...state, phase: "aborted" };
    case "reset":
      return INITIAL;
    default:
      return state;
  }
}

function fromStreamError(payload: ErrorPayload): QaFailure {
  if (payload.reason === "unauthenticated") {
    return { kind: "unauthenticated", message: payload.message };
  }
  // 타임아웃과 생성 실패는 코드가 둘 다 `llm_unavailable` 이다. 사유만 둘을 가른다.
  return {
    kind: "generation",
    message: payload.message,
    attempts: payload.attempts,
    reason: payload.reason,
  };
}

const VALIDATION_CODES = new Set(["validation_error", "empty_query", "query_too_long", "invalid_top_k"]);

function fromRequestError(error: AppError): QaFailure {
  if (error.code === NETWORK_UNREACHABLE) {
    return { kind: "network", message: error.message };
  }
  if (error.code === "llm_unauthenticated") {
    return { kind: "unauthenticated", message: error.message };
  }
  if (VALIDATION_CODES.has(error.code)) {
    return {
      kind: "validation",
      message: error.message,
      maxQueryChars: error.number("max_query_chars"),
      maxQueryTokens: error.number("max_query_tokens"),
      maxTopK: error.number("max_top_k"),
    };
  }
  return { kind: "other", code: error.code, message: error.message };
}

function isAbort(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

export function useQaStream() {
  const [state, dispatch] = useReducer(reduce, INITIAL);
  const controllerRef = useRef<AbortController | null>(null);
  const runIdRef = useRef(0);

  // 화면을 떠날 때 열린 스트림을 끊는다. 안 끊으면 서버 쪽 생성이 계속 돈다.
  useEffect(() => () => controllerRef.current?.abort(), []);

  const ask = useCallback(async (question: string, topK: number | null) => {
    // 이전 요청을 먼저 끊는다. 순서가 반대면 두 스트림이 잠시 함께 산다.
    controllerRef.current?.abort();
    const runId = ++runIdRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    const isCurrent = () => runIdRef.current === runId;

    dispatch({ type: "start", question });

    try {
      const body = await openQaStream(question, topK, controller.signal);
      if (!isCurrent()) return;

      let terminated = false;
      for await (const frame of readSseFrames(body)) {
        if (!isCurrent()) return;
        if (frame.event === "sources") {
          dispatch({ type: "sources", payload: JSON.parse(frame.data) as SourcesPayload });
        } else if (frame.event === "answer") {
          dispatch({ type: "answer", text: (JSON.parse(frame.data) as AnswerPayload).text });
        } else if (frame.event === "done") {
          terminated = true;
          dispatch({ type: "done", payload: JSON.parse(frame.data) as DonePayload });
        } else if (frame.event === "error") {
          terminated = true;
          dispatch({ type: "failed", failure: fromStreamError(JSON.parse(frame.data) as ErrorPayload) });
        }
      }

      if (!isCurrent()) return;
      if (!terminated) {
        // 종료 이벤트 없는 닫힘을 진행 중으로 남기면 사용자가 매번 타임아웃으로 추정해야 한다.
        dispatch({
          type: "failed",
          failure: { kind: "disconnected", message: "답변이 끝나기 전에 연결이 끊겼습니다" },
        });
      }
    } catch (cause) {
      if (!isCurrent()) return;
      if (isAbort(cause)) {
        dispatch({ type: "aborted" });
      } else if (cause instanceof AppError) {
        dispatch({ type: "failed", failure: fromRequestError(cause) });
      } else {
        dispatch({
          type: "failed",
          failure: { kind: "other", code: "internal_error", message: "답변을 받는 중 오류가 발생했습니다" },
        });
      }
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  }, []);

  const cancel = useCallback(() => controllerRef.current?.abort(), []);
  const reset = useCallback(() => {
    controllerRef.current?.abort();
    runIdRef.current += 1;
    dispatch({ type: "reset" });
  }, []);

  const answerText = useMemo(() => state.chunks.join(""), [state.chunks]);
  const isBusy = state.phase === "preparing" || state.phase === "streaming";

  const rejection = useMemo<QaRejection | null>(() => {
    if (state.phase !== "rejected" || !state.result) return null;
    if (state.result.finish_reason === "insufficient_evidence") return "insufficient_evidence";
    return state.sources && state.sources.target_documents === 0 ? "no_documents" : "no_relevant_evidence";
  }, [state.phase, state.result, state.sources]);

  return { state, answerText, isBusy, rejection, ask, cancel, reset };
}
