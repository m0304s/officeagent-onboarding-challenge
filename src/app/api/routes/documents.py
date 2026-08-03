"""문서 엔드포인트 — 업로드·목록·상세·삭제.

여기서 하는 일은 HTTP 관심사뿐이다. 셋이 같은 `DocumentView` 를 쓰는 이유는 같은 문서를
다른 모양으로 보여주면 소비자가 모델을 둘 들어야 하기 때문이다.
"""

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict

from app.core.documents import Document, DocumentFormat, IndexStatus, IngestionStatus
from app.core.exceptions import DocumentTooLarge
from app.services.ingestion import IngestionResult, IngestionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_NO_CONTENT = 204


def get_ingestion_service(request: Request) -> IngestionService:
    """앱 팩토리가 배선한 서비스를 꺼낸다. 모듈 전역 싱글턴을 두지 않는다."""
    return request.app.state.ingestion_service


IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]
UploadedFile = Annotated[UploadFile, File(description="수집할 문서 (.txt · .md · .pdf)")]


class DocumentView(BaseModel):
    """레지스트리에 기록된 문서 한 건.

    `index_status` 가 응답에 있어야 "올렸는데 답을 못 찾는다"가 설명 가능해진다."""

    model_config = ConfigDict(from_attributes=True)

    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    index_signature: str
    index_status: IndexStatus
    chunk_count: int
    byte_size: int
    ingested_at: datetime

    @classmethod
    def of(cls, document: Document) -> "DocumentView":
        return cls.model_validate(document)


class UploadView(DocumentView):
    """업로드 응답 — 문서 상태에 "이번 요청이 무엇을 했는가"를 더한 것."""

    status: IngestionStatus
    #: 내용이 바뀐 교체에서만 값이 있다. 재색인은 `revision` 이 그대로라 값이 없다.
    previous_revision: str | None = None

    @classmethod
    def of_result(cls, result: IngestionResult) -> "UploadView":
        return cls(
            **asdict(result.document),
            status=result.status,
            previous_revision=result.previous_revision,
        )


class DocumentListView(BaseModel):
    """목록 응답. 봉투를 씌우는 이유는 페이지네이션이 붙어도 최상위 타입이 안 바뀌게."""

    documents: list[DocumentView]
    count: int


@router.post(
    "/documents",
    status_code=HTTP_CREATED,
    response_model=UploadView,
    responses={HTTP_OK: {"description": "이미 수집된 문서의 교체·재색인·무변경"}},
    summary="문서 업로드",
)
async def upload_document(
    request: Request,
    response: Response,
    service: IngestionServiceDep,
    file: UploadedFile,
) -> UploadView:
    """문서를 수집한다 — 추출·분할·임베딩·저장까지 한 요청 안에서.

    최초 수집만 201 이다. 크기 검사가 막는 것은 수신이 아니라 파싱과 임베딩이다."""
    max_bytes = request.app.state.settings.max_upload_bytes
    if file.size and file.size > max_bytes:
        raise DocumentTooLarge(
            f"업로드 크기 상한({max_bytes:,} 바이트)을 넘었습니다",
            max_upload_bytes=max_bytes,
        )

    data = await file.read()
    result = await service.ingest(file.filename or "", data)

    if result.status is not IngestionStatus.CREATED:
        response.status_code = HTTP_OK
    _log_upload(request, result)
    return UploadView.of_result(result)


@router.get("/documents", response_model=DocumentListView, summary="문서 목록")
async def list_documents(service: IngestionServiceDep) -> DocumentListView:
    """수집된 문서 전체. `stale` 문서도 포함한다."""
    documents = await service.list_documents()
    return DocumentListView(
        documents=[DocumentView.of(document) for document in documents],
        count=len(documents),
    )


@router.get("/documents/{document_id}", response_model=DocumentView, summary="문서 상세")
async def get_document(document_id: str, service: IngestionServiceDep) -> DocumentView:
    """문서 한 건. 수집된 적 없으면 `404 not_found`."""
    return DocumentView.of(await service.get_document(document_id))


@router.delete(
    "/documents/{document_id}",
    status_code=HTTP_NO_CONTENT,
    response_class=Response,
    summary="문서 삭제",
)
async def delete_document(
    request: Request, document_id: str, service: IngestionServiceDep
) -> Response:
    """문서와 그 청크를 전부 지운다. 없으면 `404`, 본문 없이 `204` 로 끝낸다."""
    document = await service.delete(document_id)
    logger.info(
        "문서 삭제 요청을 처리했습니다",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "document_id": document_id,
            "document_filename": document.filename,
            "chunk_count": document.chunk_count,
        },
    )
    return Response(status_code=HTTP_NO_CONTENT)


def _log_upload(request: Request, result: IngestionResult) -> None:
    """업로드 한 건을 요청과 묶어 한 줄로 남긴다. 본문과 청크 내용은 싣지 않는다.

    키가 `document_filename` 인 것은 `filename` 을 `LogRecord` 가 이미 쓰기 때문이다."""
    document = result.document
    logger.info(
        "문서 업로드 완료",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "document_id": document.document_id,
            "document_filename": document.filename,
            "format": document.format.value,
            "revision": document.revision[:12],
            "byte_size": document.byte_size,
            "page_count": result.page_count,
            "chunk_count": document.chunk_count,
            "ingestion_status": result.status.value,
        },
    )
