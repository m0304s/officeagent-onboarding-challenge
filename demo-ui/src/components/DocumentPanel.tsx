import type { useDocuments } from "../hooks/useDocuments";
import { INGESTION_STATUS_LABEL, uploadErrorMessage } from "../lib/messages";
import { DocumentRow } from "./DocumentRow";
import { Dropzone } from "./Dropzone";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { EmptyState } from "./ui/EmptyState";
import styles from "./DocumentPanel.module.css";

export function DocumentPanel({ documents }: { documents: ReturnType<typeof useDocuments> }) {
  const { documents: list, loading, busy, error, lastUpload, upload, remove, dismissUpload } = documents;
  const failure = error ? uploadErrorMessage(error) : null;

  return (
    <section className={styles.panel} aria-labelledby="documents-heading">
      <div className={styles.head}>
        <h2 id="documents-heading" className={styles.heading}>
          문서
        </h2>
        <Badge tone="neutral">{list.length}건</Badge>
      </div>

      <Dropzone onFile={(file) => void upload(file)} disabled={busy} />

      {lastUpload ? (
        <div className={styles.notice} role="status">
          <div className={styles.noticeHead}>
            <Badge tone={lastUpload.status === "created" ? "ok" : "info"}>
              {INGESTION_STATUS_LABEL[lastUpload.status]}
            </Badge>
            <Button variant="ghost" onClick={dismissUpload} aria-label="업로드 결과 닫기">
              닫기
            </Button>
          </div>
          <p className={styles.noticeBody}>
            {lastUpload.filename} — 청크 {lastUpload.chunk_count.toLocaleString()}개
            {lastUpload.previous_revision ? " · 이전 판을 대체했습니다" : ""}
          </p>
        </div>
      ) : null}

      {failure ? (
        <div className={styles.error} role="alert">
          <p className={styles.errorTitle}>{failure.title}</p>
          {failure.detail ? <p className={styles.errorDetail}>{failure.detail}</p> : null}
        </div>
      ) : null}

      {loading ? (
        <p className={styles.loading}>문서 목록을 읽는 중입니다…</p>
      ) : list.length === 0 ? (
        <EmptyState title="수집된 문서가 없습니다">
          문서를 올리면 텍스트를 추출해 청크로 나눈 뒤 검색할 수 있게 색인합니다.
          <br />
          리포의 <code>sample-docs/</code> 에 시험용 문서 두 건이 있습니다.
        </EmptyState>
      ) : (
        <ul className={styles.list}>
          {list.map((document) => (
            <DocumentRow
              key={document.document_id}
              document={document}
              busy={busy}
              onDelete={(id) => void remove(id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
