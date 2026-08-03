import { useId, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";

import { Button } from "./ui/Button";
import styles from "./QuestionForm.module.css";

// 서버의 문자 수 상한과 같은 값. 넘겨도 서버가 422 로 막지만, 여기서 세어 보여 주면
// 사용자가 전송 전에 안다.
const MAX_CHARS = 1000;

export function QuestionForm({
  busy,
  onSubmit,
  onCancel,
}: {
  busy: boolean;
  onSubmit: (question: string) => void;
  onCancel: () => void;
}) {
  const fieldId = useId();
  const countId = useId();
  const [question, setQuestion] = useState("");

  const tooLong = question.length > MAX_CHARS;
  const empty = question.trim() === "";

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (busy || empty || tooLong) return;
    onSubmit(question.trim());
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 로 보내고 Shift+Enter 로 줄을 바꾼다. IME 조합 중의 Enter 는 한글 입력을
    // 확정하는 키라, 여기서 가로채면 마지막 글자가 잘린 채 전송된다.
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    submit();
  };

  return (
    <form className={styles.form} onSubmit={submit}>
      <label className={styles.label} htmlFor={fieldId}>
        질문
      </label>
      <textarea
        id={fieldId}
        className={[styles.input, tooLong ? styles.invalid : undefined].filter(Boolean).join(" ")}
        value={question}
        rows={3}
        placeholder="예) 교육비 지원은 얼마까지 받을 수 있나요?"
        aria-describedby={countId}
        aria-invalid={tooLong || undefined}
        onChange={(event) => setQuestion(event.target.value)}
        onKeyDown={handleKeyDown}
      />
      <div className={styles.foot}>
        <span id={countId} className={[styles.count, tooLong ? styles.countOver : undefined].filter(Boolean).join(" ")}>
          {question.length.toLocaleString()} / {MAX_CHARS.toLocaleString()}자
          {tooLong ? " — 상한을 넘었습니다" : ""}
        </span>
        <div className={styles.actions}>
          {busy ? (
            <Button type="button" variant="danger" onClick={onCancel}>
              중단
            </Button>
          ) : null}
          <Button type="submit" variant="primary" loading={busy} invalid={tooLong} disabled={empty || tooLong}>
            {busy ? "답변 받는 중" : "질문 보내기"}
          </Button>
        </div>
      </div>
    </form>
  );
}
