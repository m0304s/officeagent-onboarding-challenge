"""어휘 색인의 계약 (`lexical-index` 스펙).

**실물 SQLite 로 돈다.** 이 층에서 검증할 것 — BM25 의 세 성질, `bm25()` 부호 뒤집기,
`fts5vocab` 로 읽는 문서 빈도, 가상 테이블의 삭제 — 은 전부 FTS5 자체의 동작이라
대역으로 바꾸면 검증 대상이 사라진다. 파일 하나뿐이라 컨테이너도 네트워크도 필요 없다.

부호 변환은 오류를 내지 않고 **순위만 뒤집는다.** 원값을 그대로 실으면 점수가 전부 음수라
`ScoredChunk` 가 거절하겠지만, `DESC` 를 걸면 아무 데서도 걸리지 않고 가장 안 맞는 청크가
1위가 된다 — 그래서 「점수는 음수가 아니고 앞이 더 크다」가 이 파일에서 가장 중요한 단언이다.
"""

from pathlib import Path

import pytest

from app.adapters.lexical import SqliteLexicalIndex
from app.core.chunking import ChunkStrategy, get_splitter
from app.core.documents import (
    Chunk,
    ChunkLocation,
    DocumentFormat,
    StoredIndexVersion,
    TextSegment,
    identify_chunks,
)
from app.core.exceptions import StorageUnavailable

REVISION = "rev-1"
SIGNATURE = "sig-1"
SAMPLE_DOCS = Path(__file__).resolve().parents[1] / "sample-docs"


def make_chunk(document_id: str, chunk_index: int, text: str, **axes) -> Chunk:
    return Chunk(
        document_id=document_id,
        revision=axes.get("revision", REVISION),
        index_signature=axes.get("index_signature", SIGNATURE),
        chunk_index=chunk_index,
        text=text,
        location=ChunkLocation(char_start=0, char_end=max(1, len(text))),
    )


def version_of(chunk: Chunk) -> StoredIndexVersion:
    return StoredIndexVersion(chunk.document_id, chunk.revision, chunk.index_signature)


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "lexical.sqlite3"


@pytest.fixture
def index(index_path: Path) -> SqliteLexicalIndex:
    """변별력 가드를 끈 색인. 순위·점수를 재는 자리에서는 가드가 결과를 지우면 안 보인다."""
    return SqliteLexicalIndex(index_path, min_token_rarity=0.0)


@pytest.fixture
def guarded(index_path: Path) -> SqliteLexicalIndex:
    """배포 기본값(`0.3`)을 쓰는 색인. 변별력 가드 자체를 보는 테스트가 쓴다."""
    return SqliteLexicalIndex(index_path)


async def store(index: SqliteLexicalIndex, *chunks: Chunk, filename: str = "policy.txt") -> None:
    await index.add_chunks(chunks, filename=filename, document_format=DocumentFormat.TXT)


# ── 등록과 검색 ──────────────────────────────────────────────────────────


class TestStoredChunksAreFound:
    async def test_an_indexed_chunk_is_found_by_a_word_in_its_text(self, index):
        chunk = make_chunk("doc-a", 0, "교육비 지원 제도를 안내합니다")
        await store(index, chunk)

        hits = await index.search("교육비", top_k=5, versions=[version_of(chunk)])

        assert len(hits) == 1
        assert (hits[0].document_id, hits[0].revision, hits[0].index_signature) == (
            "doc-a",
            REVISION,
            SIGNATURE,
        )
        assert hits[0].chunk_index == 0
        assert hits[0].text == chunk.text

    async def test_the_stored_provenance_survives_the_round_trip(self, index):
        """출처 표기가 이 값들로 만들어진다 — 왕복에서 잃으면 인용이 성립하지 않는다."""
        chunk = Chunk(
            document_id="doc-a",
            revision=REVISION,
            index_signature=SIGNATURE,
            chunk_index=3,
            text="장애 대응 절차",
            location=ChunkLocation(char_start=10, char_end=40, page=7),
        )
        await index.add_chunks(
            [chunk], filename="guide.pdf", document_format=DocumentFormat.PDF
        )

        hit = (await index.search("장애", top_k=5, versions=[version_of(chunk)]))[0]

        assert hit.filename == "guide.pdf"
        assert hit.format is DocumentFormat.PDF
        assert hit.location == ChunkLocation(char_start=10, char_end=40, page=7)

    async def test_chunks_outside_the_target_list_are_not_found(self, index):
        mine = make_chunk("doc-a", 0, "공통 단어 교육비")
        theirs = make_chunk("doc-b", 0, "공통 단어 교육비")
        await store(index, mine, theirs)

        hits = await index.search("교육비", top_k=5, versions=[version_of(mine)])

        assert [hit.document_id for hit in hits] == ["doc-a"]

    async def test_an_empty_target_list_returns_nothing_rather_than_everything(self, index):
        await store(index, make_chunk("doc-a", 0, "교육비 지원"))

        assert await index.search("교육비", top_k=5, versions=[]) == []

    async def test_a_query_without_tokens_returns_nothing(self, index):
        chunk = make_chunk("doc-a", 0, "교육비 지원")
        await store(index, chunk)

        assert await index.search("?! ...", top_k=5, versions=[version_of(chunk)]) == []


# ── BM25 의 세 성질 ──────────────────────────────────────────────────────


class TestRankingReflectsBm25:
    async def test_scores_are_not_negative_and_the_front_is_larger(self, index):
        """`bm25()` 원값은 음수다. 부호를 안 뒤집거나 `DESC` 를 걸면 여기서 걸린다."""
        chunks = [
            make_chunk("doc-a", 0, "교육비 교육비 교육비 지원"),
            make_chunk("doc-a", 1, "교육비 지원 절차 안내 문서 본문 추가 문장 여럿"),
            make_chunk("doc-a", 2, "재택근무 신청 절차"),
        ]
        await store(index, *chunks)

        hits = await index.search("교육비", top_k=5, versions=[version_of(chunks[0])])

        scores = [hit.native_score for hit in hits]
        assert len(hits) == 2, "교육비를 담은 청크 둘이 나와야 한다"
        assert all(score >= 0 for score in scores)
        assert scores == sorted(scores, reverse=True)
        assert hits[0].chunk_index == 0, "겹침이 많은 청크가 앞에 와야 한다"

    async def test_a_rare_term_contributes_more_than_a_common_one(self, index):
        """희소성이 없으면 조사·접속어가 많은 청크가 이긴다."""
        target = make_chunk("doc-a", 0, "공지 특별상여금")
        others = [make_chunk("doc-a", position, "공지 일반 안내") for position in range(1, 12)]
        await store(index, target, *others)
        versions = [version_of(target)]

        rare = await index.search("특별상여금", top_k=5, versions=versions)
        common = await index.search("공지", top_k=5, versions=versions)

        by_common = {hit.chunk_index: hit.native_score for hit in common}
        assert rare[0].chunk_index == 0
        assert rare[0].native_score > by_common[0]

    async def test_repeating_a_term_saturates(self, index):
        """포화가 없으면 같은 단어가 반복되는 목록형 청크가 언제나 이긴다."""
        repeated = [make_chunk("doc-a", count, " ".join(["알림"] * count)) for count in (1, 2, 8)]
        fillers = [make_chunk("doc-a", 20 + n, f"무관한 본문 {n} 입니다") for n in range(10)]
        await store(index, *repeated, *fillers)

        hits = await index.search("알림", top_k=5, versions=[version_of(repeated[0])])

        scores = {hit.chunk_index: hit.native_score for hit in hits}
        assert scores[1] < scores[2] < scores[8]
        assert scores[8] < scores[1] * 8, "반복 횟수에 비례해 오르면 포화가 없는 것이다"

    async def test_the_shorter_chunk_wins_at_equal_overlap(self, index):
        """길이 정규화가 없으면 긴 청크가 항상 이긴다."""
        short = make_chunk("doc-a", 0, "연차 안내")
        long = make_chunk("doc-a", 1, "연차 " + "부가 설명 문장이 길게 이어집니다 " * 8)
        fillers = [make_chunk("doc-a", 20 + n, f"무관한 본문 {n} 입니다") for n in range(10)]
        await store(index, short, long, *fillers)

        hits = await index.search("연차", top_k=5, versions=[version_of(short)])

        assert [hit.chunk_index for hit in hits] == [0, 1]


# ── 토큰화 규약 ──────────────────────────────────────────────────────────


class TestQueriesReachTheStem:
    async def test_a_query_with_a_particle_finds_the_stem(self, index):
        chunk = make_chunk("doc-a", 0, "교육비 지원")
        await store(index, chunk)

        hits = await index.search("교육비는 얼마인가요", top_k=5, versions=[version_of(chunk)])

        assert [hit.chunk_index for hit in hits] == [0]

    async def test_an_identifier_stays_one_search_unit(self, index):
        incident = make_chunk("doc-a", 0, "P1 장애 대응")
        stage = make_chunk("doc-a", 1, "1단계 절차 안내")
        await store(index, incident, stage)

        hits = await index.search("P1", top_k=5, versions=[version_of(incident)])

        assert hits[0].chunk_index == 0
        assert stage.chunk_index not in [hit.chunk_index for hit in hits[:1]]

    async def test_case_and_character_width_do_not_block_a_match(self, index):
        chunk = make_chunk("doc-a", 0, "배포는 v2 파이프라인을 씁니다")
        await store(index, chunk)

        hits = await index.search("Ｖ２", top_k=5, versions=[version_of(chunk)])

        assert [hit.chunk_index for hit in hits] == [0]


# ── 변별력 가드 ──────────────────────────────────────────────────────────


class TestOnlyDiscriminatingOverlapCounts:
    async def test_a_chunk_matched_only_by_a_common_token_does_not_rise(self, guarded, index):
        """이 가드가 없으면 일상 질문이 흔한 토큰 하나로 청크를 끌어올린다."""
        chunks = [make_chunk("doc-a", position, "공지 사항 안내") for position in range(12)]
        await store(index, *chunks)

        assert await guarded.search("공지", top_k=5, versions=[version_of(chunks[0])]) == []

    async def test_an_everyday_question_against_the_sample_docs_is_empty(self, guarded, index):
        versions = await index_sample_docs(index)

        assert await guarded.search("오늘 서울 날씨 어때?", top_k=5, versions=versions) == []

    async def test_one_rare_token_is_enough_to_rise(self, guarded, index):
        rare = make_chunk("doc-a", 0, "공지 사항 특별상여금 지급")
        common = [make_chunk("doc-a", position, "공지 사항 안내") for position in range(1, 12)]
        await store(index, rare, *common)

        hits = await guarded.search(
            "공지 사항 특별상여금", top_k=5, versions=[version_of(rare)]
        )

        assert [hit.chunk_index for hit in hits] == [0]

    async def test_the_sample_docs_regression_queries_survive_the_guard(self, guarded, index):
        """하한이 정상 질의를 죽이면 하이브리드 검색이 꺼진 것과 구별되지 않는다."""
        versions = await index_sample_docs(index)

        for query in (
            "교육비는 얼마까지 지원되나요?",
            "재택근무는 일주일에 몇 번 할 수 있나요?",
            "PR을 머지하려면 승인이 몇 명 필요한가요?",
            "P1 장애가 나면 몇 분 안에 원인을 파악해야 하나요?",
        ):
            assert await guarded.search(query, top_k=5, versions=versions), query

    async def test_an_empty_result_is_not_an_error(self, guarded, index):
        chunks = [make_chunk("doc-a", position, "공지 사항 안내") for position in range(12)]
        await store(index, *chunks)

        assert await guarded.search("공지", top_k=5, versions=[version_of(chunks[0])]) == []


async def index_sample_docs(index: SqliteLexicalIndex) -> list[StoredIndexVersion]:
    """`sample-docs/` 두 건을 실제 청킹 구성으로 넣는다 — 문서를 베끼면 리포와 어긋난다."""
    split = get_splitter(ChunkStrategy.RECURSIVE)
    versions = []
    for filename in ("company-policy.txt", "development-guide.md"):
        text = (SAMPLE_DOCS / filename).read_text(encoding="utf-8")
        chunks = identify_chunks(
            split((TextSegment(text=text),), 200, 40),
            document_id=filename,
            revision=REVISION,
            index_signature=SIGNATURE,
        )
        await index.add_chunks(chunks, filename=filename, document_format=DocumentFormat.TXT)
        versions.append(StoredIndexVersion(filename, REVISION, SIGNATURE))
    return versions


# ── 제거와 열거 ──────────────────────────────────────────────────────────


class TestRemovalSharesTheVectorAxes:
    async def test_removing_by_document_leaves_other_documents_alone(self, index):
        mine = make_chunk("doc-a", 0, "교육비 지원")
        also_mine = make_chunk("doc-a", 1, "교육비 신청")
        theirs = make_chunk("doc-b", 0, "교육비 정산")
        await store(index, mine, also_mine, theirs)

        removed = await index.delete_document("doc-a")

        assert removed == 2
        assert await index.search("교육비", top_k=5, versions=[version_of(mine)]) == []
        assert len(await index.search("교육비", top_k=5, versions=[version_of(theirs)])) == 1

    async def test_removing_by_signature_keeps_the_other_generation(self, index):
        """재색인은 `revision` 이 그대로인 채 일어난다 — 축을 하나만 좁히는 경로가 필요하다."""
        old = make_chunk("doc-a", 0, "교육비 지원", index_signature="sig-old")
        new = make_chunk("doc-a", 0, "교육비 지원", index_signature="sig-new")
        await store(index, old, new)

        removed = await index.delete_document("doc-a", index_signature="sig-old")

        assert removed == 1
        assert await index.search("교육비", top_k=5, versions=[version_of(old)]) == []
        assert len(await index.search("교육비", top_k=5, versions=[version_of(new)])) == 1

    async def test_removing_by_revision_keeps_the_other_revision(self, index):
        old = make_chunk("doc-a", 0, "교육비 지원", revision="rev-old")
        new = make_chunk("doc-a", 0, "교육비 지원", revision="rev-new")
        await store(index, old, new)

        assert await index.delete_document("doc-a", revision="rev-old") == 1
        assert len(await index.search("교육비", top_k=5, versions=[version_of(new)])) == 1

    async def test_removing_nothing_reports_zero(self, index):
        await store(index, make_chunk("doc-a", 0, "교육비 지원"))

        assert await index.delete_document("doc-없음") == 0

    async def test_stored_versions_are_enumerated_without_duplicates(self, index):
        await store(
            index,
            make_chunk("doc-a", 0, "본문 하나"),
            make_chunk("doc-a", 1, "본문 둘"),
            make_chunk("doc-b", 0, "본문 셋", revision="rev-2"),
        )

        assert await index.list_stored_versions() == [
            StoredIndexVersion("doc-a", REVISION, SIGNATURE),
            StoredIndexVersion("doc-b", "rev-2", SIGNATURE),
        ]

    async def test_counting_narrows_by_the_same_axes(self, index):
        await store(
            index,
            make_chunk("doc-a", 0, "본문 하나"),
            make_chunk("doc-a", 1, "본문 둘", revision="rev-2"),
            make_chunk("doc-b", 0, "본문 셋"),
        )

        assert await index.count_chunks() == 3
        assert await index.count_chunks("doc-a") == 2
        assert await index.count_chunks("doc-a", revision="rev-2") == 1
        assert await index.count_chunks(index_signature="없는-서명") == 0

    async def test_registering_the_same_chunk_twice_stores_one_row(self, index):
        chunk = make_chunk("doc-a", 0, "교육비 지원")
        await store(index, chunk)
        await store(index, chunk)

        hits = await index.search("교육비", top_k=5, versions=[version_of(chunk)])

        assert len(hits) == 1
        assert await index.count_chunks("doc-a") == 1

    async def test_reregistering_replaces_the_text(self, index):
        first = make_chunk("doc-a", 0, "교육비 지원")
        await store(index, first)
        await store(index, make_chunk("doc-a", 0, "재택근무 안내"))

        assert await index.search("교육비", top_k=5, versions=[version_of(first)]) == []
        assert len(await index.search("재택근무", top_k=5, versions=[version_of(first)])) == 1


# ── 결정성 ───────────────────────────────────────────────────────────────


class TestTheSameQueryGivesTheSameList:
    async def test_repeating_a_search_gives_an_identical_list(self, index):
        chunks = [make_chunk("doc-a", position, "교육비 지원 안내") for position in range(4)]
        await store(index, *chunks)
        versions = [version_of(chunks[0])]

        first = await index.search("교육비", top_k=5, versions=versions)
        second = await index.search("교육비", top_k=5, versions=versions)

        assert first == second

    async def test_the_order_of_the_target_list_does_not_change_the_result(self, index):
        a = make_chunk("doc-a", 0, "교육비 지원")
        b = make_chunk("doc-b", 0, "교육비 정산")
        c = make_chunk("doc-c", 0, "교육비 신청")
        await store(index, a, b, c)

        forward = await index.search(
            "교육비", top_k=5, versions=[version_of(a), version_of(b), version_of(c)]
        )
        backward = await index.search(
            "교육비", top_k=5, versions=[version_of(c), version_of(b), version_of(a)]
        )

        assert forward == backward

    async def test_ties_are_broken_by_identity(self, index):
        """같은 본문이라 점수가 같다 — 순서가 저장 순서에 기대면 여기서 흔들린다."""
        later = make_chunk("doc-b", 0, "교육비 지원")
        earlier = make_chunk("doc-a", 0, "교육비 지원")
        await store(index, later, earlier)

        hits = await index.search(
            "교육비", top_k=5, versions=[version_of(later), version_of(earlier)]
        )

        assert [hit.document_id for hit in hits] == ["doc-a", "doc-b"]

    async def test_the_result_is_cut_at_top_k(self, index):
        chunks = [make_chunk("doc-a", position, "교육비 지원") for position in range(6)]
        await store(index, *chunks)

        hits = await index.search("교육비", top_k=2, versions=[version_of(chunks[0])])

        assert len(hits) == 2


# ── 접근 실패 ────────────────────────────────────────────────────────────


class TestFailureIsNotDisguisedAsEmptiness:
    """빈 목록과 색인 장애를 뭉개면 어휘 retriever 가 죽은 것을 아무도 알 수 없다.

    융합은 빈 목록을 정상 입력으로 받으므로, 하이브리드 검색이 꺼진 채로 몇 주가 지나도
    응답은 계속 `200` 이다.
    """

    @pytest.fixture
    def broken(self, tmp_path: Path) -> SqliteLexicalIndex:
        path = tmp_path / "not-a-database.sqlite3"
        path.write_bytes("sqlite3 파일이 아닌 바이트".encode())
        return SqliteLexicalIndex(path)

    async def test_a_failing_search_raises_instead_of_returning_nothing(self, broken):
        with pytest.raises(StorageUnavailable):
            await broken.search(
                "교육비", top_k=5, versions=[StoredIndexVersion("doc-a", REVISION, SIGNATURE)]
            )

    async def test_a_failing_write_raises(self, broken):
        with pytest.raises(StorageUnavailable):
            await store(broken, make_chunk("doc-a", 0, "교육비 지원"))

    async def test_a_failing_count_raises(self, broken):
        with pytest.raises(StorageUnavailable):
            await broken.count_chunks()

    async def test_a_missing_store_is_created_on_first_use(self, tmp_path: Path):
        """색인 저장소가 없는 상태에서도 기동은 성공해야 한다 — 생성은 첫 사용의 몫이다."""
        nested = tmp_path / "fresh-volume" / "lexical.sqlite3"
        index = SqliteLexicalIndex(nested)

        assert not nested.parent.exists(), "생성이 기동으로 앞당겨졌다"
        assert await index.count_chunks() == 0
        assert nested.exists()
