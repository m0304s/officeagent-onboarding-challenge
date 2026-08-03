// 문서 목록의 단일 소유자. 업로드·삭제 뒤 목록을 서버에서 다시 읽어 화면이 낙관적
// 추정을 들고 있지 않게 한다.

import { useCallback, useEffect, useState } from "react";

import { AppError, deleteDocument, listDocuments, uploadDocument } from "../api/client";
import type { DocumentView, UploadView } from "../api/types";

export function useDocuments() {
  const [documents, setDocuments] = useState<DocumentView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<AppError | null>(null);
  const [lastUpload, setLastUpload] = useState<UploadView | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const list = await listDocuments(signal);
      setDocuments(list.documents);
      setError(null);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      if (cause instanceof AppError) setError(cause);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  const upload = useCallback(
    async (file: File) => {
      setBusy(true);
      setError(null);
      try {
        const result = await uploadDocument(file);
        setLastUpload(result);
        await refresh();
        return result;
      } catch (cause) {
        // 실패한 업로드는 목록을 건드리지 않는다 — 재조회도 하지 않는다.
        if (cause instanceof AppError) setError(cause);
        setLastUpload(null);
        return null;
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const remove = useCallback(
    async (documentId: string) => {
      setBusy(true);
      setError(null);
      try {
        await deleteDocument(documentId);
        setLastUpload(null);
        await refresh();
        return true;
      } catch (cause) {
        if (cause instanceof AppError) setError(cause);
        return false;
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const dismissUpload = useCallback(() => setLastUpload(null), []);

  return { documents, loading, busy, error, lastUpload, upload, remove, refresh, dismissUpload };
}
