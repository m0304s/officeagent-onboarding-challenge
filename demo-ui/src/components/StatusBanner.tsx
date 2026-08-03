import type { QaFailure, QaRejection } from "../hooks/useQaStream";
import styles from "./StatusBanner.module.css";

interface Copy {
  title: string;
  detail: string;
  tone: "danger" | "warn" | "neutral";
}

const REASON_LABEL: Record<"timeout" | "generation_failed", string> = {
  timeout: "시간 초과",
  generation_failed: "생성 실패",
};

function failureCopy(failure: QaFailure): Copy {
  switch (failure.kind) {
    case "validation": {
      const limits = [
        failure.maxQueryChars === null ? null : `문자 수 상한 ${failure.maxQueryChars.toLocaleString()}자`,
        failure.maxQueryTokens === null ? null : `토큰 상한 ${failure.maxQueryTokens.toLocaleString()}개`,
        failure.maxTopK === null ? null : `근거 수 상한 ${failure.maxTopK}개`,
      ].filter((item): item is string => item !== null);
      return {
        tone: "warn",
        title: failure.message,
        detail: limits.length > 0 ? limits.join(" · ") : "요청 값을 줄여서 다시 시도해 주세요.",
      };
    }
    case "unauthenticated":
      return {
        tone: "warn",
        title: "답변 생성기가 인증되지 않았습니다",
        detail:
          "재시도해도 결과는 같습니다 — 자격증명이 있어야 합니다. 문서 수집과 근거 검색은 인증 없이도 정상 동작합니다.",
      };
    case "generation":
      return {
        tone: "danger",
        title: `답변 생성에 실패했습니다 (${REASON_LABEL[failure.reason]})`,
        detail: `${failure.attempts}회 시도했습니다. 근거는 위에 그대로 남아 있습니다.`,
      };
    case "network":
      return {
        tone: "danger",
        title: "API 서버에 닿지 못했습니다",
        detail: "서버가 떠 있는지 확인해 주세요 — `docker compose up`.",
      };
    case "disconnected":
      return {
        tone: "danger",
        title: "답변이 끝나기 전에 연결이 끊겼습니다",
        detail: "도착한 조각까지만 남아 있습니다. 다시 물어보세요.",
      };
    default:
      return { tone: "danger", title: failure.message, detail: `오류 코드: ${failure.code}` };
  }
}

const REJECTION_COPY: Record<QaRejection, Copy> = {
  no_documents: {
    tone: "neutral",
    title: "수집된 문서가 없습니다",
    detail: "왼쪽 패널에서 문서를 먼저 올려 주세요. 근거가 없으면 답변을 만들지 않습니다.",
  },
  no_relevant_evidence: {
    tone: "neutral",
    title: "관련 근거를 찾지 못했습니다",
    detail: "문서는 있지만 이 질문과 충분히 가까운 대목이 없습니다. 답변을 지어내지 않고 여기서 멈춥니다.",
  },
  insufficient_evidence: {
    tone: "neutral",
    title: "근거가 답하기에 부족합니다",
    detail: "찾은 근거만으로는 확답할 수 없다고 판정했습니다. 문서 밖 지식으로 채우지 않습니다.",
  },
};

export function StatusBanner({
  failure,
  rejection,
  aborted = false,
}: {
  failure?: QaFailure | null;
  rejection?: QaRejection | null;
  aborted?: boolean;
}) {
  const copy = aborted
    ? { tone: "neutral" as const, title: "중단했습니다", detail: "도착한 조각까지만 남아 있습니다." }
    : failure
      ? failureCopy(failure)
      : rejection
        ? REJECTION_COPY[rejection]
        : null;

  if (!copy) return null;

  return (
    <div className={[styles.banner, styles[copy.tone]].join(" ")} role={failure ? "alert" : "status"}>
      <p className={styles.title}>{copy.title}</p>
      <p className={styles.detail}>{copy.detail}</p>
    </div>
  );
}
