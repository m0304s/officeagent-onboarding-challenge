import { useQaStream } from "../hooks/useQaStream";
import { AnswerStream } from "./AnswerStream";
import { QuestionForm } from "./QuestionForm";
import { SourceList } from "./SourceList";
import { StatusBanner } from "./StatusBanner";
import { EmptyState } from "./ui/EmptyState";
import styles from "./QaConsole.module.css";

/**
 * 보조 기술에 알릴 문구. 조각마다 통지하면 낭독이 끊기지 않으므로 전이만 말한다 —
 * 이 값이 바뀌는 시점이 곧 통지 시점이다.
 */
function liveMessage(qa: ReturnType<typeof useQaStream>): string {
  switch (qa.state.phase) {
    case "preparing":
      return "근거를 찾는 중입니다";
    case "streaming":
      return `근거 ${qa.state.sources?.count ?? 0}건을 찾았습니다. 답변을 생성 중입니다`;
    case "answered":
      return `답변이 완료되었습니다. 인용 ${qa.state.result?.citations.length ?? 0}건.`;
    case "rejected":
      return "근거가 부족해 답변하지 않았습니다";
    case "failed":
      return "답변 생성이 실패했습니다";
    case "aborted":
      return "답변을 중단했습니다";
    default:
      return "";
  }
}

export function QaConsole() {
  const qa = useQaStream();
  const { state, answerText, isBusy, rejection } = qa;
  const message = liveMessage(qa);

  return (
    <section className={styles.console} aria-labelledby="qa-heading">
      <h2 id="qa-heading" className={styles.heading}>
        질의응답
      </h2>

      <QuestionForm busy={isBusy} onSubmit={(question) => void qa.ask(question, null)} onCancel={qa.cancel} />

      {/* 실패는 같은 줄에서 알림으로 승격한다 — 조용한 정중함보다 도착이 우선이다. */}
      <p className="srOnly" role={state.phase === "failed" ? "alert" : "status"} aria-live="polite">
        {message}
      </p>

      {state.phase === "idle" ? (
        <EmptyState title="질문을 입력하면 근거부터 찾습니다">
          검색된 근거가 먼저 도착하고, 그 근거만으로 답변이 조각 단위로 흘러나옵니다.
          <br />
          근거가 없으면 답변을 만들지 않고 그 사실을 알려 줍니다.
        </EmptyState>
      ) : null}

      {state.sources && state.sources.count > 0 ? <SourceList sources={state.sources} /> : null}

      <AnswerStream phase={state.phase} text={answerText} result={state.result} />

      <StatusBanner
        failure={state.phase === "failed" ? state.failure : null}
        rejection={rejection}
        aborted={state.phase === "aborted"}
      />
    </section>
  );
}
