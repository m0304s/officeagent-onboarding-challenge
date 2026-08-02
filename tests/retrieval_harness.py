"""검색 서비스 테스트 하네스.

검색 테스트는 **수집을 거쳐 만들어진 상태** 위에서만 의미가 있다. 대역에 청크를 손으로
심으면 레지스트리와 벡터 스토어가 어긋난 상태를 쉽게 만들 수 있고, 그러면 검증 대상인
"둘이 어긋났을 때 검색이 무엇을 하는가"를 테스트가 스스로 조작하게 된다. 그래서 두
서비스를 **같은 대역 위에** 세워 두고, 상태는 실제 수집 경로로 만든다.

기본 하한은 `0.0` 이다. 페이크 임베더의 벡터는 텍스트 해시라 점수에 의미가 없으므로,
구조(순서·필터·K)를 재는 테스트가 하한에 걸리면 그건 검증이 아니라 잡음이다. 하한 자체를
재는 테스트만 `searching_with(min_score=...)` 로 값을 명시한다.

**임베더는 프로토콜로 받는다.** 기본값은 페이크지만 품질 테스트
(`test_retrieval_quality.py`)가 같은 하네스에 **실물 모델**을 꽂는다 — 구조를 재는 층과
품질을 재는 층이 같은 배선 위에 서야, 한쪽에서만 통과하는 상태가 생기지 않는다.
"""

from dataclasses import dataclass

from app.adapters.parsers import ParserRegistry, default_parsers
from app.adapters.protocols import Embedder
from app.core.chunking import CHUNK_STRATEGY_VERSION, ChunkStrategy
from app.core.documents import Document, derive_index_signature
from app.services.ingestion import IngestionService
from app.services.retrieval import RetrievalService
from tests.stubs import FakeEmbedder, StubDocumentRegistry, StubVectorStore

#: 여러 청크로 쪼개지는 길이의 한국어 문서 둘. 주제를 갈라 둔 이유는 "다른 문서의 청크가
#: 섞였는가"를 본문으로 눈에 보이게 확인하기 위해서다.
POLICY = (
    "사내 복리후생 안내\n\n"
    "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 하며 영수증을 첨부해야 합니다.\n"
    "재택근무는 주 2회까지 가능합니다. 팀장 승인 후 사내 시스템에 등록합니다.\n"
    "연차는 입사 첫해 11일, 이듬해부터 15일이 부여됩니다. 미사용 연차는 이월되지 않습니다.\n"
    "건강검진은 매년 1회 지원하며 지정 병원에서 받습니다. 배우자 검진도 포함됩니다.\n"
    "경조사비는 결혼 100만원, 조사 50만원을 지급합니다. 경조 휴가는 별도로 부여됩니다.\n"
    "야근 식대는 1인당 1만원까지 지원하며 법인카드로 결제합니다. 주말 근무도 같습니다.\n"
    "도서 구입비는 월 5만원까지 지원합니다. 업무와 관련된 도서에 한합니다.\n"
    "동호회 활동비는 분기당 인당 3만원을 지원합니다. 5인 이상이면 등록할 수 있습니다.\n"
    "장기근속 포상은 5년 단위로 지급하며 유급 휴가 5일이 함께 부여됩니다.\n"
)
GUIDE = (
    "개발 가이드\n\n"
    "코드 리뷰는 최소 1명의 승인을 받아야 머지할 수 있습니다. 리뷰는 24시간 안에 응답합니다.\n"
    "브랜치는 main 에서 따고 feature 접두사를 붙입니다. 머지는 스쿼시로 합니다.\n"
    "배포는 태그를 올리면 자동으로 진행됩니다. 롤백은 이전 태그를 다시 배포합니다.\n"
    "P1 장애는 30분 안에 원인을 파악하고 담당자를 지정합니다. 사후 회고는 3일 안에 작성합니다.\n"
    "테스트는 커밋 전에 전부 통과해야 합니다. 실패한 채로 올린 브랜치는 리뷰하지 않습니다.\n"
    "의존성 추가는 팀 논의를 거칩니다. 라이선스와 유지보수 상태를 함께 확인합니다.\n"
    "로그에는 사용자 입력 원문을 남기지 않습니다. 식별자와 집계값만 기록합니다.\n"
    "문서와 코드가 어긋나면 문서를 먼저 고칩니다. 어긋난 문서는 없는 것보다 나쁩니다.\n"
)
SHORT = "재택근무는 주 2회까지 가능합니다.\n"


@dataclass
class Harness:
    """같은 대역 위에 선 수집·검색 서비스 한 쌍."""

    ingestion: IngestionService
    retrieval: RetrievalService
    embedder: Embedder
    store: StubVectorStore
    registry: StubDocumentRegistry
    index_signature: str
    _top_k: int
    _min_score: float

    async def ingest(self, filename: str, text: str) -> Document:
        return (await self.ingestion.ingest(filename, text.encode())).document

    def searching_with(self, *, top_k: int | None = None, min_score: float | None = None):
        """같은 대역을 보되 설정만 다른 검색 서비스.

        하한 비교("하한을 내리면 걸러졌던 청크가 나타난다")는 **같은 저장소 상태**에서
        설정만 갈아야 성립한다. 앱을 다시 만들면 저장소도 새로 생겨 비교가 무의미해진다.
        """
        return RetrievalService(
            self.embedder,
            self.store,
            self.registry,
            index_signature=self.index_signature,
            top_k=self._top_k if top_k is None else top_k,
            min_score=self._min_score if min_score is None else min_score,
        )

    def chunk_text(self, document_id: str, chunk_index: int) -> str:
        """저장된 청크의 **실제 본문**.

        순위 단언은 "질의 문자열을 청크 본문과 똑같이 두면 벡터가 일치해 1위"라는 성질에
        기대는데, 그 본문은 정규화와 분할을 거친 뒤의 값이라 원문에서 짐작할 수 없다.
        """
        return self.store.chunks_of(document_id)[chunk_index].text


def make_harness(
    *,
    embedder: Embedder | None = None,
    vector_store: StubVectorStore | None = None,
    registry: StubDocumentRegistry | None = None,
    size: int = 200,
    overlap: int = 40,
    top_k: int = 5,
    min_score: float = 0.0,
) -> Harness:
    embedder = embedder or FakeEmbedder()
    store = vector_store or StubVectorStore()
    registry = registry or StubDocumentRegistry()
    # 배선(`create_app`)이 하는 일 그대로 — 한 번 유도해 **두 서비스에 같은 값**을 준다.
    signature = derive_index_signature(
        embedder_signature=embedder.signature,
        chunk_strategy=ChunkStrategy.RECURSIVE.value,
        chunk_strategy_version=CHUNK_STRATEGY_VERSION,
        chunk_size=size,
        chunk_overlap=overlap,
    )
    return Harness(
        ingestion=IngestionService(
            ParserRegistry(default_parsers()),
            embedder,
            store,
            registry,
            index_signature=signature,
            chunk_strategy=ChunkStrategy.RECURSIVE,
            chunk_size=size,
            chunk_overlap=overlap,
            embedding_batch_size=64,
            concurrency=2,
        ),
        retrieval=RetrievalService(
            embedder,
            store,
            registry,
            index_signature=signature,
            top_k=top_k,
            min_score=min_score,
        ),
        embedder=embedder,
        store=store,
        registry=registry,
        index_signature=signature,
        _top_k=top_k,
        _min_score=min_score,
    )
