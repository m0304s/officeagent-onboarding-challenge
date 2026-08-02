"""문서 엔드포인트 — 업로드·목록·상세·삭제.

이 계층이 하는 일은 HTTP 관심사뿐이다. 업로드 크기 판정, 상태 코드 선택, 응답 모양.
무엇을 저장하고 무엇을 지우는가는 전부 `IngestionService` 가 정한다.

**응답에 청크 본문을 싣지 않는다.** 저장 전에는 청크를 응답으로 돌려주는 것 말고
결과를 보여줄 방법이 없어 그렇게 했지만, 이제 청크는 벡터 스토어에 있다. 업로드
응답과 상세 조회가 같은 문서를 서로 다른 모양으로 보여주면 소비자가 모델을 둘 들어야
하므로, 셋 다 같은 `DocumentView` 를 쓰고 업로드만 `status` 를 덧붙인다.
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

    `index_signature` 와 `index_status` 가 응답에 있는 이유는 이 둘이 **검색 가능
    여부**를 설명하는 유일한 값이기 때문이다. `stale` 인 문서는 목록에 있지만 청크가
    없어 검색되지 않는다 — 그 사실이 응답에 없으면 "올렸는데 답을 못 찾는다"가
    설명 불가능한 현상이 된다.
    """

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
    """목록 응답.

    배열을 그대로 돌려주지 않는 이유는 이후에 페이지네이션이나 집계가 붙을 때
    최상위 타입이 바뀌어야 하기 때문이다. 봉투가 있으면 항목만 늘리면 된다.
    """

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

    본문 파싱은 프레임워크에 맡긴다 — `file` 파트가 없거나 multipart 가 아니면 프레임워크
    검증이 422 `validation_error` 로 끝낸다. 라우터가 직접 판정할 이유가 없다.

    **상태 코드는 무엇이 생겼는가로 갈린다.** 최초 수집만 `201` 이다. 교체·재색인·무변경은
    이미 있던 리소스를 갱신하거나 그대로 두는 것이라 `200` 이며, 무엇이 일어났는지는
    본문의 `status` 가 말한다.

    **크기 상한의 성격**: `UploadFile` 은 핸들러가 불릴 때 본문이 **이미 수신된** 뒤다
    (Starlette 이 1 MiB 를 넘으면 디스크로 스풀하므로 메모리는 본문 크기에 묶이지
    않는다). 따라서 이 검사가 막는 것은 수신이 아니라 **파싱과 임베딩**이다 — 큰 파일이
    CPU 를 쓰는 일은 없지만, 바이트 자체는 일단 받는다. 수신 단계에서 끊으려면 리버스
    프록시의 `client_max_body_size` 같은 앞단 상한이 함께 있어야 한다.
    """
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
    """문서와 그 청크를 전부 지운다. 수집된 적 없으면 `404 not_found`.

    본문 없이 `204` 로 끝낸다 — 지워진 리소스를 응답에 다시 실을 이유가 없다.
    """
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
    """업로드 한 건이 무엇을 했는지를 요청과 묶어 한 줄로 남긴다.

    `extra` 항목은 JSON 로그의 최상위 키가 된다. 본문이나 청크 내용은 싣지 않는다 —
    문서 내용이 로그로 새어 나가면 그 자체가 유출이다.

    키 이름이 `document_filename` 인 이유: `filename` 은 `LogRecord` 가 이미 쓰는 이름
    (로그를 남긴 **소스 파일**)이라 `extra` 로 넘기면 로깅이 `KeyError` 로 거절한다.
    """
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
