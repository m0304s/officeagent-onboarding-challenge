import { useId, useRef, useState } from "react";
import type { DragEvent } from "react";

import styles from "./Dropzone.module.css";

const ACCEPT = ".txt,.md,.pdf";

/**
 * 파일 선택과 드래그 앤 드롭 둘 다. 선택 트리거를 `label` + 숨은 `input` 으로 두는 이유는
 * 그 조합이 키보드 조작과 접근 가능한 이름을 브라우저에게서 공짜로 받기 때문이다.
 */
export function Dropzone({
  onFile,
  disabled = false,
}: {
  onFile: (file: File) => void;
  disabled?: boolean;
}) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  // 자식 요소를 지날 때마다 dragleave 가 나므로 깊이를 세지 않으면 테두리가 깜빡인다.
  const depth = useRef(0);

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    depth.current = 0;
    setDragging(false);
    if (disabled) return;
    const file = event.dataTransfer.files.item(0);
    if (file) onFile(file);
  };

  return (
    <div
      className={[styles.zone, dragging ? styles.dragging : undefined, disabled ? styles.disabled : undefined]
        .filter(Boolean)
        .join(" ")}
      onDragEnter={(event) => {
        event.preventDefault();
        depth.current += 1;
        if (!disabled) setDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => {
        event.preventDefault();
        depth.current -= 1;
        if (depth.current <= 0) setDragging(false);
      }}
      onDrop={handleDrop}
    >
      <input
        ref={inputRef}
        id={inputId}
        className="srOnly"
        type="file"
        accept={ACCEPT}
        disabled={disabled}
        onChange={(event) => {
          const file = event.target.files?.item(0);
          if (file) onFile(file);
          // 같은 파일을 연속으로 고를 수 있게 값을 비운다 — 안 비우면 change 가 안 난다.
          event.target.value = "";
        }}
      />
      <label className={styles.label} htmlFor={inputId}>
        문서 파일 선택
      </label>
      <p className={styles.hint}>{dragging ? "여기에 놓으세요" : "또는 이곳으로 파일을 끌어다 놓으세요"}</p>
      <p className={styles.formats}>.txt · .md · .pdf</p>
    </div>
  );
}
