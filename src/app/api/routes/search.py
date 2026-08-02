"""검색 엔드포인트 — 질의 하나에 근거 청크 상위 K개.

**답변을 만들지 않는다.** 이 엔드포인트가 돌려주는 것은 근거 후보까지이고, 그 근거로
답할 수 있는지의 판정은 답변 생성의 몫이다. 그래서 응답에 답변 필드도 거절 문구도 없다.
답이 이상할 때 원인이 검색인지 생성인지 가르는 관측 지점이 이것이라, 나중에 `/qa` 가
생겨도 이 엔드포인트는 남는다.

**`GET` 이 아니라 `POST` 인 이유**는 부수 효과가 아니라 요청 모양이다. 한국어 질의가 URL
인코딩되면 로그와 오류 메시지에서 읽을 수 없게 되고, 길이 상한이 서버·프록시의 URL 길이
제한과 뒤섞인다. 검색이 아무것도 바꾸지 않는다는 사실은 메서드가 아니라 문서로 말한다.

이 계층이 하는 일은 HTTP 관심사뿐이다 — 요청 검증, 응답 모양, 로깅. 무엇을 대상으로
삼고 무엇을 버리는가는 전부 `RetrievalService` 가 정한다.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.adapters.protocols import Embedder
from app.config import Settings
from app.core.documents import DocumentFormat
from app.core.exceptions import EmptyQuery, InvalidTopK, QueryTooLong
from app.core.retrieval import ScoredChunk
from app.services.retrieval import RetrievalResult, RetrievalService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


def get_retrieval_service(request: Request) -> RetrievalService:
    """앱 팩토리가 배선한 서비스를 꺼낸다. 모듈 전역 싱글턴을 두지 않는다."""
    return request.app.state.retrieval_service


def get_embedder(request: Request) -> Embedder:
    """토큰 상한을 판정하려면 API 계층이 임베더에 닿아야 한다.

    상한이 **모델이 선언한 값**이라 설정에서 읽을 수 없고, 판정이 질의 벡터 계산보다
    앞서야 하므로 서비스 안으로 밀어 넣을 수도 없다 — 거부된 질의가 임베딩을 유발하지
    않으려면 그 판정이 임베딩 이전, 즉 이 계층에서 끝나야 한다.
    """
    return request.app.state.embedder


def get_settings_of(request: Request) -> Settings:
    return request.app.state.settings


RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
SettingsDep = Annotated[Settings, Depends(get_settings_of)]


class SearchRequest(BaseModel):
    """질의 한 건.

    **`top_k` 의 하한만 여기 있다.** `1` 이상이라는 사실은 어느 배포에서나 같아 요청
    스키마에 속하고, 스키마에 두면 OpenAPI 문서에 그대로 드러나 소비자가 요청을 보내기
    전에 안다. 상한은 기동 시점에야 정해지는 배포 설정이라 스키마에 넣을 수 없어
    라우터가 설정과 대조한다 — 검증 위치가 사실의 성격을 따라간다.
    """

    query: str = Field(description="질문 한 문장")
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="돌려받을 근거 청크 수. 주지 않으면 서버 기본값을 쓴다",
    )


class SearchResultView(BaseModel):
    """근거 청크 하나 — 정체성·본문·출처·점수.

    결과 하나가 **스스로 출처를 말한다.** 인용 한 줄을 만들려고 문서를 다시 조회하지
    않아도 되게 파일명·포맷·리비전을 함께 싣는다.

    `page` 는 PDF 에서 온 청크만 채운다. 그때의 `char_start`·`char_end` 는 **그 쪽 안의**
    오프셋이다 — PDF 의 문서 전역 오프셋은 실재하지 않는 연결 문자열을 가정해야 나오는
    값이라 수집이 쓰지 않았고, 검색이 지어낼 수도 없다.

    `score` 는 유사도다 — 클수록 질의에 가깝고 `[0, 1]` 이다. 저장소가 돌려주는 거리는
    어댑터가 이미 뒤집어 두었으므로, 소비자가 지표 방향을 알 필요가 없다.
    """

    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    chunk_index: int
    text: str
    score: float
    char_start: int
    char_end: int
    page: int | None = None

    @classmethod
    def of(cls, chunk: ScoredChunk) -> "SearchResultView":
        return cls(
            document_id=chunk.document_id,
            filename=chunk.filename,
            format=chunk.format,
            revision=chunk.revision,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            score=chunk.score,
            char_start=chunk.location.char_start,
            char_end=chunk.location.char_end,
            page=chunk.location.page,
        )


class SearchResponse(BaseModel):
    """검색 응답.

    봉투를 쓰는 것은 `GET /documents` 의 관습 그대로다 — 배열을 최상위에 두면 나중에
    항목을 더할 때 타입이 바뀐다. `top_k` 를 되싣는 이유는 요청이 생략했을 때 **실제로
    적용된 값**이 응답에만 드러나기 때문이다.
    """

    query: str
    top_k: int
    results: list[SearchResultView]
    count: int


@router.post("/search", response_model=SearchResponse, summary="근거 청크 검색")
async def search(
    request: Request,
    body: SearchRequest,
    service: RetrievalServiceDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
) -> SearchResponse:
    """질의와 가까운 청크를 유사도 내림차순으로 최대 K개.

    **검색 대상은 지금 유효한 청크뿐이다** — 이전 리비전, 이전 색인 구성, 삭제된 문서,
    `stale` 문서의 청크는 저장소에 남아 있더라도 결과에 나타나지 않는다.

    유사도 하한에 걸려 결과가 비는 것은 **오류가 아니다.** `200` 과 빈 목록으로 끝난다 —
    "답할 수 없다"는 판정은 본문을 읽어야 하는 종류라 이 계층의 몫이 아니다.
    """
    _reject_invalid(body, embedder, settings)
    await _reject_beyond_the_input_window(body.query, embedder, settings)

    result = await service.search(body.query, top_k=body.top_k)

    _log_search(request, result)
    return SearchResponse(
        query=result.query,
        top_k=result.top_k,
        results=[SearchResultView.of(chunk) for chunk in result.chunks],
        count=result.count,
    )


def _reject_invalid(body: SearchRequest, embedder: Embedder, settings: Settings) -> None:
    """토크나이저에 닿기 전에 끝나는 판정들 — 내용 없음, 문자 수, K 상한.

    셋 다 **질의 벡터가 계산되기 전에** 끝난다. 토큰 계산(블로킹)보다 먼저 두는 이유는
    순서에 의미가 있어서가 아니라, 공짜로 판정되는 것을 스레드 하나 빌린 뒤에 판정할
    이유가 없기 때문이다.
    """
    if not body.query.strip():
        raise EmptyQuery("질의가 비어 있습니다")

    # 이 상한이 막는 것은 조용한 절단이 아니라 **임의 길이 입력이 토크나이저에 들어가는
    # 것**이다. 절단은 아래의 토큰 가드가 막는다 — 문자 수만으로 절단까지 막으려면
    # 상한이 101자가 되어 평범한 질문까지 거부된다(같은 글자 수라도 문자 종류에 따라
    # 토큰 수가 아홉 배 가까이 달라진다).
    if len(body.query) > settings.retrieval_max_query_chars:
        raise _too_long(
            f"질의가 문자 수 상한({settings.retrieval_max_query_chars}자)을 넘었습니다",
            embedder,
            settings,
        )

    # 상한만 여기서 본다. 하한(`>= 1`)은 요청 모델이 이미 `validation_error` 로 끝냈다.
    if body.top_k is not None and body.top_k > settings.retrieval_max_top_k:
        raise InvalidTopK(
            f"top_k 는 {settings.retrieval_max_top_k} 이하여야 합니다",
            max_top_k=settings.retrieval_max_top_k,
        )


async def _reject_beyond_the_input_window(
    query: str, embedder: Embedder, settings: Settings
) -> None:
    """**조용한 절단을 막는 진짜 가드다.**

    문자 수를 통과한 질의를 실제로 인코딩되는 문자열 기준으로 세어(역할 접두사와 특수
    토큰을 포함한다 — 본문만 세면 그 몫만큼 과소 계산되어 상한 바로 아래 질의가 잘린다)
    모델이 선언한 입력 창을 넘으면 거부한다. 자르지 않는 이유는 질문을 반으로 잘라 각각
    검색하면 그건 다른 질문이고, 조용히 자르면 사용자는 전부 반영됐다고 믿기 때문이다.

    토큰 계산은 토크나이저 호출이라 **블로킹**이다. 수집이 `_guard_tokens` 를 스레드풀로
    내보내는 것과 같은 이유로 여기서도 오프로드한다 — 이벤트 루프 위에서 돌리면 긴 질의
    하나가 다른 요청의 처리를 막는다.
    """
    tokens = await asyncio.to_thread(embedder.count_query_tokens, query)
    if tokens > embedder.max_input_tokens:
        raise _too_long(
            f"질의가 임베딩 입력 창({embedder.max_input_tokens} 토큰)을 넘었습니다",
            embedder,
            settings,
        )


def _too_long(reason: str, embedder: Embedder, settings: Settings) -> QueryTooLong:
    """**어느 쪽에 걸렸든 두 상한을 함께 싣는다.**

    걸린 쪽만 실으면 소비자가 코드는 하나인데 항목이 둘인 응답을 받게 되어 분기를
    들어야 한다. 둘 다 클라이언트가 미리 알 수 없는 값이라는 점도 같다 — 문자 상한은
    배포 설정이고, 토큰 상한은 임베딩 모델이 선언한 입력 창이다. 무엇에 걸렸는지는
    `message` 가 말한다.
    """
    return QueryTooLong(
        reason,
        max_query_chars=settings.retrieval_max_query_chars,
        max_query_tokens=embedder.max_input_tokens,
    )


def _log_search(request: Request, result: RetrievalResult) -> None:
    """질의 한 건이 무엇을 했는지 요청과 묶어 한 줄로.

    **질의 문자열과 청크 본문은 싣지 않는다** — 수집이 문서 내용을 로그에 남기지 않는
    것과 같은 이유다. 질문 자체가 개인정보일 수 있고, 청크 본문은 문서 내용 그 자체다.

    `target_documents` 가 있어야 빈 결과의 이유가 갈린다 — 대상이 0이면 올린 문서가
    없거나 전부 `stale` 인 것이고, 대상이 있는데 0이면 하한에 걸린 것이다.
    """
    logger.info(
        "검색 요청을 처리했습니다",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "top_k": result.top_k,
            "result_count": result.count,
            "top_score": result.top_score,
            "target_documents": result.target_documents,
        },
    )
