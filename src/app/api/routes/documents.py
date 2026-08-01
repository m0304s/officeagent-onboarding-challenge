"""문서 엔드포인트.

> **현재 이 라우터는 업로드를 받아 추출·청킹까지만 한다.** 임베딩·벡터 저장·레지스트리
> 커밋이 아직 없어 **청크는 어디에도 남지 않는다.** 그래서 응답이 `201 Created` 가
> 아니라 `200` 이고, 본문에 `index_signature`·`index_status`·`status` 가 없다 —
> 저장하지 않았는데 만들어졌다고 응답하면 그 응답이 거짓이 된다. 저장 단계가 들어오면
> 상태 코드와 본문이 spec 이 정한 모양으로 바뀐다.

`GET`·`DELETE` 도 아직 없다. 조회할 레지스트리가 없기 때문이다.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.core.exceptions import DocumentTooLarge
from app.services.ingestion import ExtractionResult, IngestionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])


def get_ingestion_service(request: Request) -> IngestionService:
    """앱 팩토리가 배선한 서비스를 꺼낸다. 모듈 전역 싱글턴을 두지 않는다."""
    return request.app.state.ingestion_service


IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]
UploadedFile = Annotated[UploadFile, File(description="수집할 문서 (.txt · .md · .pdf)")]


@router.post("/documents")
async def upload_document(
    request: Request,
    service: IngestionServiceDep,
    file: UploadedFile,
) -> dict[str, Any]:
    """문서를 업로드해 추출·청킹한다.

    본문 파싱은 프레임워크에 맡긴다 — `file` 파트가 없거나 multipart 가 아니면 프레임워크
    검증이 422 `validation_error` 로 끝낸다. 라우터가 직접 판정할 이유가 없다.

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
    result = await service.extract_chunks(file.filename or "", data)
    _log_extraction(request, result)
    return _serialize(result)


def _log_extraction(request: Request, result: ExtractionResult) -> None:
    """무엇이 어떻게 잘렸는지를 한 줄로 남긴다.

    `extra` 항목은 JSON 로그의 최상위 키가 된다. 본문이나 청크 내용은 싣지 않는다 —
    문서 내용이 로그로 새어 나가면 그 자체가 유출이다.

    키 이름이 `document_filename` 인 이유: `filename` 은 `LogRecord` 가 이미 쓰는 이름
    (로그를 남긴 **소스 파일**)이라 `extra` 로 넘기면 로깅이 `KeyError` 로 거절한다.
    """
    logger.info(
        "문서 추출 완료",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "document_id": result.document_id,
            "document_filename": result.filename,
            "format": result.format.value,
            "revision": result.revision[:12],
            "byte_size": result.byte_size,
            "page_count": result.page_count,
            "chunk_count": result.chunk_count,
        },
    )


def _serialize(result: ExtractionResult) -> dict[str, Any]:
    return {
        "document_id": result.document_id,
        "filename": result.filename,
        "format": result.format.value,
        "revision": result.revision,
        "byte_size": result.byte_size,
        "page_count": result.page_count,
        "chunk_count": result.chunk_count,
        "chunks": [
            {
                "chunk_index": index,
                "page": chunk.location.page,
                "char_start": chunk.location.char_start,
                "char_end": chunk.location.char_end,
                "text": chunk.text,
            }
            for index, chunk in enumerate(result.chunks)
        ],
    }


