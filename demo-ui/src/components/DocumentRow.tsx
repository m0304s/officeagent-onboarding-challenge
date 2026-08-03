import { useState } from "react";

import type { DocumentView } from "../api/types";
import { formatBytes, formatDateTime } from "../lib/format";
import { INDEX_STATUS_LABEL } from "../lib/messages";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import styles from "./DocumentRow.module.css";

export function DocumentRow({
  document,
  busy,
  onDelete,
}: {
  document: DocumentView;
  busy: boolean;
  onDelete: (documentId: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);

  return (
    <li className={styles.row}>
      <div className={styles.head}>
        <span className={styles.filename} title={document.filename}>
          {document.filename}
        </span>
        <Badge tone={document.index_status === "indexed" ? "ok" : "warn"} dot>
          {INDEX_STATUS_LABEL[document.index_status]}
        </Badge>
      </div>

      <dl className={styles.meta}>
        <div>
          <dt>형식</dt>
          <dd>{document.format}</dd>
        </div>
        <div>
          <dt>청크</dt>
          <dd>{document.chunk_count.toLocaleString()}개</dd>
        </div>
        <div>
          <dt>크기</dt>
          <dd>{formatBytes(document.byte_size)}</dd>
        </div>
        <div>
          <dt>수집 시각</dt>
          <dd>{formatDateTime(document.ingested_at)}</dd>
        </div>
      </dl>

      {confirming ? (
        <div className={styles.confirm} role="group" aria-label={`${document.filename} 삭제 확인`}>
          <span className={styles.confirmText}>이 문서와 청크를 모두 지웁니다.</span>
          <div className={styles.actions}>
            <Button variant="danger" loading={busy} onClick={() => onDelete(document.document_id)}>
              삭제 확정
            </Button>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              취소
            </Button>
          </div>
        </div>
      ) : (
        <div className={styles.actions}>
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => setConfirming(true)}
            aria-label={`${document.filename} 삭제`}
          >
            삭제
          </Button>
        </div>
      )}
    </li>
  );
}
