// 오류 코드 → 사람이 읽을 문구. 코드별로 갈라 두는 이유는 사용자가 할 일이 다르기
// 때문이다 — 포맷이 문제면 다른 파일을, 용량이 문제면 다른 크기를 골라야 한다.

import { AppError, NETWORK_UNREACHABLE } from "../api/client";
import type { IndexStatus, IngestionStatus } from "../api/types";
import { formatBytes } from "./format";

export interface UploadMessage {
  title: string;
  detail: string | null;
}

export function uploadErrorMessage(error: AppError): UploadMessage {
  switch (error.code) {
    case "unsupported_document_format": {
      const formats = error.strings("supported_formats");
      return {
        title: "지원하지 않는 파일 형식입니다",
        detail: formats.length > 0 ? `수집 가능한 형식: ${formats.join(", ")}` : null,
      };
    }
    case "document_too_large": {
      const max = error.number("max_upload_bytes");
      return {
        title: "파일이 업로드 상한을 넘습니다",
        detail: max === null ? null : `상한은 ${formatBytes(max)} 입니다`,
      };
    }
    case "empty_document":
      return { title: "빈 파일입니다", detail: "내용이 있는 문서를 올려 주세요" };
    case "no_extractable_text": {
      const pages = error.number("page_count");
      return {
        title: "추출할 텍스트가 없습니다",
        detail:
          pages === null
            ? "스캔 이미지만 든 PDF 일 수 있습니다"
            : `${pages}쪽을 읽었지만 텍스트가 없습니다 — 스캔 이미지만 든 PDF 일 수 있습니다`,
      };
    }
    case "document_parse_error":
      return { title: "파일을 읽지 못했습니다", detail: "파일이 손상됐는지 확인해 주세요" };
    case "storage_unavailable":
      return { title: "저장소에 닿지 못했습니다", detail: "벡터 스토어가 떠 있는지 확인해 주세요" };
    case NETWORK_UNREACHABLE:
      return { title: "API 서버에 닿지 못했습니다", detail: "서버가 떠 있는지 확인해 주세요" };
    default:
      return { title: error.message, detail: null };
  }
}

export const INGESTION_STATUS_LABEL: Record<IngestionStatus, string> = {
  created: "최초 수집",
  replaced: "내용이 바뀌어 교체",
  reindexed: "재색인",
  unchanged: "무변경",
};

export const INDEX_STATUS_LABEL: Record<IndexStatus, string> = {
  indexed: "검색 가능",
  stale: "재색인 필요",
};
