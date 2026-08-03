"""검색 오케스트레이션.

```
질의 임베딩 → 대상 집합 확정(레지스트리) → 저장소 질의 → 현재성 재검증 → 하한 적용 → 조립
```

순서가 이 파일의 전부다. 특히 **대상 집합이 저장소 질의보다 앞**이라는 점이 계약이다 —
저장소에는 검색되면 안 되는 청크가 남아 있을 수 있고(되돌리기가 닿지 못한 실패, 확정
뒤의 정리 실패), 그 잔여물이 무해한 유일한 근거가 "검색이 현재 값으로만 필터한다"는 이
약속이다. 사후 필터로 바꾸면 걸러낸 만큼 결과가 줄어 `top_k` 가 의미를 잃고, 잔여 청크가
많은 상태에서는 유효한 청크가 K 밖으로 밀려 결과가 통째로 빌 수 있다.

**답변을 만들지 않는다.** 이 서비스가 하는 판정은 "이 청크를 다음 단계에 보여줄 가치가
있는가"(유사도 하한)까지이고, "이 근거로 답할 수 있는가"는 본문을 읽어야 하는 판정이라
답변 생성의 몫이다. 여기서 거절 문구를 만들면 프롬프트가 그 판정을 뒤집을 수 없게 된다.

계층 규칙: 이 모듈은 어댑터의 **프로토콜**만 알고 구현체를 모른다. 셋 전부 생성자로
주입받는다.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.adapters.protocols import DocumentRegistry, Embedder, VectorStore
from app.core.documents import Document, StoredIndexVersion
from app.core.retrieval import ScoredChunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalResult:
    """검색 한 건이 무엇을 돌려주는가.

    `chunks` 만으로도 API 응답은 만들 수 있지만 `target_documents` 를 함께 싣는다 —
    결과가 비었을 때 **왜 비었는지**가 그 값으로 갈리기 때문이다. 대상이 0이면 올린
    문서가 없거나 전부 `stale` 인 것이고, 대상이 있는데 0이면 하한에 걸린 것이다.
    로그가 그 둘을 구분하지 못하면 "검색이 안 된다"는 신고에서 확인할 것이 없다.
    """

    query: str
    top_k: int
    chunks: tuple[ScoredChunk, ...]
    target_documents: int

    @property
    def count(self) -> int:
        return len(self.chunks)

    @property
    def top_score(self) -> float | None:
        """1위 점수. 결과가 없으면 `None` — `0.0` 으로 뭉개면 "무관해서 0점"과 구분되지 않는다."""
        return self.chunks[0].score if self.chunks else None


class RetrievalService:
    """질의 하나를 받아 지금 유효한 청크 중 가까운 것부터 최대 K개를 돌려준다."""

    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        registry: DocumentRegistry,
        *,
        index_signature: str,
        top_k: int,
        min_score: float,
    ) -> None:
        self._embedder = embedder
        self._store = vector_store
        self._registry = registry
        # 수집이 청크를 찍을 때 쓴 것과 **같은 값**이어야 한다. 그래서 배선이 한 번
        # 유도해 두 서비스에 주입한다 — 각자 유도하면 재료 목록이 둘이 되고, 한쪽만
        # 고쳐진 순간 방금 올린 문서가 검색되지 않으면서 어디에도 오류가 남지 않는다.
        self._index_signature = index_signature
        self._top_k = top_k
        self._min_score = min_score

    async def search(self, query: str, *, top_k: int | None = None) -> RetrievalResult:
        """질의 한 건. `top_k` 를 주면 설정 기본값을 대신한다.

        질의 문자열의 유효성(빈 질의·길이 상한·K 경계)은 **API 계층이 이미 판정했다.**
        여기서 다시 보지 않는 이유는 그 판정에 설정과 임베더의 입력 창이 필요한데,
        거부된 요청이 임베딩을 유발하지 않으려면 그 판정이 임베딩 **이전**, 즉 이
        메서드 밖에서 끝나야 하기 때문이다.
        """
        effective_k = self._top_k if top_k is None else top_k

        # **질의용 경로로 계산한다.** `embed_documents([query])[0]` 도 벡터를 내고
        # 오류도 나지 않지만, 역할에 따라 입력을 다르게 다루는 모델에서는 점수 분포가
        # 통째로 이동한다. 하한 기본값이 그 분포를 재서 정한 값이므로 경로가 어긋나면
        # 하한이 조용히 무효가 된다 — 무엇이 나빠졌는지가 어디에도 드러나지 않는다.
        embedding = await self._embedder.embed_query(query)

        current = await self._current_versions()
        if not current:
            # 대상이 없으면 저장소를 **건드리지 않는다.** 빈 목록을 넘겨도 어댑터가
            # 같은 판정을 하지만, 여기서 끊어야 "문서가 하나도 없다"가 저장소 왕복
            # 비용조차 만들지 않는다.
            return RetrievalResult(query=query, top_k=effective_k, chunks=(), target_documents=0)

        scored = await self._store.query(
            embedding,
            top_k=effective_k,
            versions=[StoredIndexVersion.of(document) for document in current.values()],
        )

        fresh = await self._drop_superseded(scored, current)
        kept = tuple(chunk for chunk in fresh if chunk.score >= self._min_score)

        return RetrievalResult(
            query=query,
            top_k=effective_k,
            chunks=kept,
            target_documents=len(current),
        )

    # ── 대상 집합 ───────────────────────────────────────────────────────

    async def _current_versions(self) -> dict[str, Document]:
        """지금 검색해도 되는 문서만 `document_id → 레코드` 로.

        판정은 레코드가 한다(`Document.is_searchable_under`) — 수집의 `stale` 처리와
        재색인 판정이 같은 축들을 보므로, 여기서 따로 구현하면 두 벌이 되고 한쪽만
        고쳐진 순간 수집이 유효하다고 여기는 문서와 검색이 찾는 문서가 어긋난다.

        리비전 축이 조건에 없는 이유는 레코드가 이미 "현재"만 들고 있어 자동으로
        따라오기 때문이다. 그 가정이 질의 사이에 깨질 수 있다는 것이 아래 재검증이
        존재하는 이유다.
        """
        documents = await self._registry.list_all()
        return {
            document.document_id: document
            for document in documents
            if document.is_searchable_under(self._index_signature)
        }

    # ── 현재성 재검증 ───────────────────────────────────────────────────

    async def _drop_superseded(
        self, scored: Sequence[ScoredChunk], filtered_with: dict[str, Document]
    ) -> list[ScoredChunk]:
        """질의 뒤에 밀려난 리비전의 결과를 버린다.

        레지스트리 읽기와 저장소 질의 **사이는 잠겨 있지 않다.** 수집의 교체 순서가
        `저장 → 커밋 → 이전 세대 정리` 이므로, 그 사이에 커밋이 끼면 검색은 방금 밀려난
        리비전을 필터에 넣고 정리가 아직 안 돌았으면 그것이 그대로 결과가 된다.

        **삭제 경로는 이미 안전하다** — 삭제는 반대 순서(`청크 삭제 → 레지스트리 삭제`)라
        청크를 받았다면 그 시점에 삭제가 아직 성공 응답을 내지 않았다. 여기서 열리는
        것은 교체·재색인 경로뿐이지만, 재검증은 두 경우를 구분하지 않고 같은 판정을 한다.

        **재시도가 아니라 드롭이다.** 재시도는 지속적인 업로드 아래에서 끝나지 않을 수
        있어 횟수 한계와 그 한계에 걸렸을 때의 동작을 또 정해야 한다. 반면 드롭의 관측
        결과는 "이전 세대 정리가 이미 돌았다"와 **완전히 같다** — 그 문서가 이번 검색에
        기여하지 않는다. 이미 존재하는 결과 모양이라 경로가 둘로 갈리지 않는다.

        **창을 좁힐 뿐 닫지는 못한다.** 조립 뒤 응답이 전달되기까지 사이의 커밋은 어떤
        방법으로도 막을 수 없다. 그래서 계약은 "결과를 조립하는 시점"까지만 보장하고,
        `revision` 을 근거로 무언가를 **영속화**하는 소비자는 그 시점에 현재성을 스스로
        다시 확인해야 한다.
        """
        contributors = list(dict.fromkeys(chunk.document_id for chunk in scored))
        if not contributors:
            return list(scored)

        # 기여한 문서만 다시 읽는다 — 보통 한두 건이다. 전체 목록을 다시 읽으면
        # 결과에 없는 문서의 변경까지 기다리는 셈이 된다.
        records = await asyncio.gather(
            *(self._registry.get(document_id) for document_id in contributors)
        )

        superseded = {
            document_id
            for document_id, record in zip(contributors, records, strict=True)
            if not self._is_still_current(record, filtered_with[document_id])
        }
        if not superseded:
            return list(scored)

        logger.info(
            "검색 도중 바뀐 문서의 결과를 버렸습니다",
            extra={"superseded_documents": sorted(superseded)},
        )
        return [chunk for chunk in scored if chunk.document_id not in superseded]

    @staticmethod
    def _is_still_current(record: Document | None, filtered_with: Document) -> bool:
        """필터에 쓴 삼중항이 지금도 그 문서의 현재 값인가.

        `None` 은 삭제가 완료됐다는 뜻이므로 당연히 아니다. 삭제 경로가 이미 안전하다는
        사실과 무관하게 같은 판정을 하는 이유는, 두 경우를 가르는 분기가 하나 줄기
        때문이다 — 그 분기의 조건이 수집의 삭제 순서에 대한 가정이라 수집이 순서를
        바꾸면 조용히 틀린다.

        비교 기준을 현재 설정이 아니라 **필터에 실제로 쓴 레코드**로 두는 이유는 질문이
        "지금 검색 가능한가"가 아니라 "우리가 찾은 그 세대가 아직 현재인가"이기
        때문이다. 두 값은 같지만(대상 집합이 이미 현재 서명으로 걸렀다) 의도가 다르다.
        """
        return record is not None and record.is_up_to_date(
            revision=filtered_with.revision,
            index_signature=filtered_with.index_signature,
        )
