"""어댑터 계약.

프로토콜은 **실제 소비자가 생기는 change에서** 정의한다. 소비자 없이 미리 정한 인터페이스는
사용처가 생기는 순간 거의 반드시 틀린 것으로 드러나기 때문이다. 그래서 여기 있는 것은
지금까지 실제로 쓰이는 계약뿐이고, `CacheStore`(get/set/invalidate) 처럼 아직 소비자가
없는 인터페이스는 정의하지 않는다.

프로토콜이 주고받는 **값 객체는 `core/`에 둔다.** `HealthProbe`(adapters) ↔ `ProbeResult`
(core)와 같은 배치다. 청킹이 `TextSegment`를 소비하는데 `core/`가 `adapters/`를 import
하면 계층이 역전된다.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.core.documents import (
    Chunk,
    Document,
    DocumentFormat,
    ExtractedDocument,
    StoredIndexVersion,
)
from app.core.models import ProbeResult
from app.core.retrieval import ScoredChunk


@runtime_checkable
class HealthProbe(Protocol):
    """의존성 하나의 도달 가능 여부를 보고한다.

    구현체는 예외를 던져도 된다. 서비스 계층이 잡아 해당 프로브만 비정상으로 기록한다.
    다만 스스로 판별 가능한 실패는 예외 대신 `ProbeResult`로 돌려주는 편이 사유 요약이
    구체적이라 진단에 낫다.
    """

    name: str

    async def check(self) -> ProbeResult: ...


@runtime_checkable
class DocumentParser(Protocol):
    """업로드된 바이트에서 텍스트를 추출한다.

    **동기다.** 두 구현 모두 CPU 바운드이므로 `async def` 로 선언하면 "이건 논블로킹"
    이라는 거짓말이 되고, 구현자가 이벤트 루프 위에서 그냥 돌려도 타입상 아무 문제가
    없어 보인다. 동기로 선언하면 호출부가 오프로드를 **의식할 수밖에 없다** — 서비스가
    명시적으로 스레드풀에 넘긴다.

    **경로가 아니라 바이트를 받는다.** 업로드는 크기 상한 안에서 이미 메모리에 올라와
    있다. 경로를 받게 하면 임시 파일 생성·정리 책임이 생기고 테스트마다 파일을 만들어야
    한다.

    구현체는 자기 경계에서 라이브러리 예외를 `DocumentParseError` 로 바꿔 던진다.
    라우터까지 `pymupdf.FileDataError` 같은 것이 새면 계층 경계가 무의미해지고, 내부
    예외 메시지가 응답에 노출된다.

    `formats` 가 복수인 이유: 텍스트 파서 하나가 `.txt` 와 `.md` 를 함께 다룬다. 둘의
    추출 방식은 같지만 응답의 `format` 값은 달라야 하므로, 파서에 단일 `format` 속성을
    두면 둘 중 하나가 거짓이 된다. 어느 포맷으로 들어왔는지는 레지스트리가 판정한다.
    """

    formats: frozenset[DocumentFormat]

    def parse(self, data: bytes) -> ExtractedDocument: ...


@runtime_checkable
class Embedder(Protocol):
    """텍스트를 벡터로 만든다.

    **역할 접두사는 어댑터 안에 갇혀 있다.** e5 계열 모델은 입력에 `passage: `/`query: `
    를 요구하는데, 그 규약은 모델 고유의 것이다. 상위 계층은 `embed_documents` 와
    `embed_query` 두 메서드만 보고 접두사의 존재를 모른다 — 모델을 바꾸면 규약도
    함께 바뀌므로, 상위 계층이 접두사를 알면 모델 교체가 상위 계층까지 번진다.

    `signature` 는 그 캡슐화를 유지한 채 **색인 서명의 재료**를 꺼내는 유일한 통로다.
    "같은 벡터를 낼 것인가"를 결정하는 사실 — 모델 식별자·차원·정규화 여부·접두사
    규약 — 을 전부 아는 곳이 어댑터뿐이기 때문이다. 사람이 읽을 수 있는 문자열로
    두어, 서명이 달라졌을 때 무엇이 달라졌는지 로그에서 바로 보이게 한다.

    `dimension`·`max_input_tokens`·`signature` 는 **모델을 로딩하지 않고도** 읽을 수
    있어야 한다. 색인 서명은 수집 경로가 시작되기 전에 필요한데, 서명을 읽는 일이
    모델 로딩을 유발하면 선로딩 실패 시의 백스톱(첫 호출 로딩)이 무의미해진다.
    구현체는 값을 선언해 두고 로딩 시점에 실제 모델과 어긋나지 않는지 확인한다.

    `warm_up` 은 기동 시 미리 준비하라는 요청이다. **프로토콜에 두는 이유는 배선
    때문이다** — 앱 팩토리는 이 프로토콜만 알고 구현체를 모른다. `isinstance` 로
    구체 어댑터를 확인하고 부르면 배선이 구현체를 알게 되어 계층 규약이 깨진다.
    준비할 것이 없는 구현체는 아무것도 하지 않으면 된다.

    **`warm_up` 이 실패해도 서비스는 떠야 한다.** 호출자가 그 실패를 삼키므로,
    구현체는 실패를 숨기지 말고 그대로 올린다 — 무엇이 준비되지 않았는지는 로그로
    남아야 하고, 판단은 호출자의 몫이다.

    **토큰 계산도 역할별로 둘이다.** `count_document_tokens`/`count_query_tokens` 가
    `embed_documents`/`embed_query` 와 짝을 이룬다. 역할마다 실제로 인코딩되는 문자열이
    다르므로(접두사 길이가 다르다) 하나로 뭉치면 둘 중 한쪽 계산이 반드시 틀리고,
    그 틀림은 **상한 바로 아래 입력이 조용히 잘리는** 방식으로만 드러난다. 어느 역할의
    수를 세는지가 이름에 있어야 호출부가 잘못 고를 수 없다. 접두사 문자열 자체는
    여전히 어댑터 밖으로 나가지 않는다 — 역할이 둘이라는 사실만 드러나는데, 그건
    `embed_*` 가 이미 드러내고 있다.

    `count_*` 만 **동기**다. 토크나이저 호출은 블로킹이므로 `DocumentParser.parse`
    와 같은 이유로 동기로 선언한다 — 호출부가 오프로드를 의식할 수밖에 없게 만든다.
    인코딩(`embed_*`)은 반대로 async 인데, 배치마다 호출되는 자리라 호출부가 매번
    감싸면 중복만 늘고 어댑터가 배치 사이 양보까지 책임지는 편이 자연스럽다.
    """

    dimension: int
    max_input_tokens: int
    signature: str

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    def count_document_tokens(self, text: str) -> int: ...

    def count_query_tokens(self, text: str) -> int: ...

    async def warm_up(self) -> None: ...


@runtime_checkable
class VectorStore(Protocol):
    """청크와 벡터를 보관하고, 벡터 하나에 가까운 청크를 돌려준다.

    **`query` 는 벡터를 받는다 — 질의 문자열이 아니다.** 문자열을 받게 하면 어댑터가
    임베더를 알아야 하고, 저장소가 임베딩 모델에 의존하는 순간 둘 중 하나를 갈아끼울 때
    다른 하나가 딸려 온다. `add_chunks` 가 텍스트가 아니라 벡터를 받는 것과 같은 규율이다.

    **검색 대상은 호출자가 삼중항 목록으로 정한다.** 저장소에는 검색되면 안 되는 청크가
    남아 있을 수 있으므로(되돌리기가 닿지 못한 실패, 확정 뒤의 정리 실패), 무엇이 지금
    유효한지는 레지스트리를 아는 서비스만 답할 수 있다. **빈 목록은 "대상 없음"이고
    "조건 없음"이 아니다** — 구현체는 저장소를 건드리지 않고 빈 결과를 돌려준다.

    `document_ids` 같은 별도 범위 필터를 두지 않는 이유는 소비자가 없어서다. 필요해지면
    `versions` 를 만드는 쪽에서 좁히면 되고, 이 인터페이스가 이미 범위 지정의 일반형이다.

    **점수는 유사도로 나간다** — 클수록 가깝다. 저장소가 거리를 돌려주더라도 구현체가
    `[0, 1]` 유사도로 바꿔 넣는다. 그 변환이 구현체 안에만 있어야 저장소를 바꿔 거리
    지표가 달라져도 상위 계층의 비교 방향이 조용히 뒤집히지 않는다.

    프로토콜은 async 이고 오프로드는 **구현체 안에서** 한다 — 파서와 반대 방향인데
    이유가 다르다. 파서는 CPU 바운드에 수백 ms~초라 호출부가 오프로드를 의식해야
    실수를 막을 수 있고, 저장소는 짧은 파일 I/O 라 호출부가 매번 감싸면 중복만 는다.

    `add_chunks` 는 **원자적이지 않다.** 벡터 스토어의 배치 쓰기에는 트랜잭션이
    없어, 계약에 원자성을 적어도 구현이 지킬 수단이 없다. 지킬 수 없는 약속을 계약에
    적는 대신 교체 순서를 아는 서비스가 되돌리기를 책임진다.
    """

    async def add_chunks(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        *,
        filename: str,
        document_format: DocumentFormat,
    ) -> None: ...

    async def delete_document(
        self,
        document_id: str,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        """조건에 맞는 청크를 지우고 지운 개수를 돌려준다.

        `revision`·`index_signature` 를 주지 않으면 그 문서의 **모든** 조합을 지운다
        (문서 삭제). 하나만 주면 그 축으로만 좁힌다 — 재색인은 `revision` 이 그대로인
        채 일어나므로 "이전 서명 청크만" 지우는 경로가 실제로 필요하다.
        """
        ...

    async def count_chunks(
        self,
        document_id: str | None = None,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int: ...

    async def list_stored_versions(self) -> list[StoredIndexVersion]:
        """저장된 `(document_id, revision, index_signature)` 조합 전체.

        기동 정리가 "레지스트리가 가리키지 않는 청크"를 찾는 데 쓴다. 레지스트리만
        보면 잔여 청크의 존재 자체를 알 수 없다.
        """
        ...

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[ScoredChunk]:
        """`versions` 에 든 삼중항의 청크 중 벡터에 가까운 것부터 최대 `top_k` 개.

        점수 **내림차순**이고, 결과 수가 `top_k` 보다 적을 수 있다(대상이 그만큼뿐일
        때). 모자란 자리를 채우지 않는다.

        `versions` 가 비면 저장소를 건드리지 않고 빈 목록을 돌려준다.
        """
        ...


@runtime_checkable
class DocumentRegistry(Protocol):
    """문서 한 건의 현재 상태 — "지금 유효한 리비전은 무엇인가"의 **단일 답**.

    벡터 스토어 메타데이터에서 유도하지 않는 이유가 여기 있다. 교체 도중에는 두
    리비전의 청크가 공존하므로 그 순간 답이 둘이 되고, 청크 전체의 메타데이터를 한
    번에 바꾸는 원자적 연산도 없다.

    `commit` 은 단일 트랜잭션이어야 한다. 이 커밋이 성립하는 순간이 곧 교체가 확정되는
    순간이다.
    """

    async def get(self, document_id: str) -> Document | None: ...

    async def list_all(self) -> list[Document]: ...

    async def commit(self, document: Document) -> None: ...

    async def delete(self, document_id: str) -> Document | None:
        """지우고 지워진 레코드를 돌려준다. 없었으면 `None`.

        지워진 레코드를 돌려주는 이유는 호출자가 벡터 스토어 정리에 필요한 값을 그
        레코드에서 얻기 때문이다. 삭제 전에 따로 조회하게 하면 그 사이에 값이 바뀔 수 있다.
        """
        ...
