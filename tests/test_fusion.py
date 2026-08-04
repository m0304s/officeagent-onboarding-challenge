"""RRF 융합의 계약 (`rank-fusion` 스펙).

점수의 마지막 비트까지 보는 단언이 여럿 있다 — 만장일치 1위의 `1.0` 과 전달 순서 무관성은
근사 비교로 물러서면 뜻이 사라지는 요구라서다.
"""

from dataclasses import dataclass

from app.core.fusion import DEFAULT_RRF_K, FusionInput, fuse


@dataclass(frozen=True)
class Item:
    """융합이 항목에 요구하는 최소한만 가진 대역."""

    document_id: str
    chunk_index: int = 0
    native_score: float = 0.0


def item(document_id: str, *, chunk_index: int = 0, native_score: float = 0.0) -> Item:
    return Item(document_id=document_id, chunk_index=chunk_index, native_score=native_score)


def ranked(name: str, *items: Item, weight: float = 1.0) -> FusionInput[Item]:
    return FusionInput(name=name, weight=weight, items=items)


def ids(fused) -> list[str]:
    return [entry.item.document_id for entry in fused]


A, B, C, D = item("a"), item("b"), item("c"), item("d")


class TestRankIsTheOnlyEvidence:
    def test_an_item_in_both_lists_outranks_items_in_one(self):
        """양쪽이 인정한 항목이 위로 오는 것이 이 융합의 전부다."""
        fused = fuse([ranked("dense", A, B), ranked("lexical", C, A)])

        assert ids(fused)[0] == "a"

    def test_native_scores_do_not_change_the_order(self):
        """서로 다른 retriever 의 점수는 단위도 분포도 달라, 크기를 쓰면 추정이 필요해진다."""
        small = fuse([ranked("dense", item("a", native_score=0.01), item("b", native_score=0.0))])
        large = fuse([ranked("dense", item("a", native_score=980.0), item("b", native_score=1.0))])

        assert ids(small) == ids(large) == ["a", "b"]
        assert [entry.score for entry in small] == [entry.score for entry in large]

    def test_a_heavier_list_pulls_its_item_up(self):
        fused = fuse([ranked("dense", A, weight=1.0), ranked("lexical", B, weight=2.0)])

        assert ids(fused) == ["b", "a"]


class TestFusionIsDeterministic:
    def test_repeated_fusion_returns_the_same_order_and_scores(self):
        inputs = [ranked("dense", A, B, C), ranked("lexical", C, A)]
        first = fuse(inputs)

        for _ in range(10):
            again = fuse(inputs)
            assert ids(again) == ids(first)
            assert [entry.score for entry in again] == [entry.score for entry in first]

    def test_delivery_order_does_not_change_the_result(self):
        dense = ranked("dense", A, B)
        lexical = ranked("lexical", C, A)

        forward = fuse([dense, lexical])
        backward = fuse([lexical, dense])

        assert ids(forward) == ids(backward)
        assert [entry.score for entry in forward] == [entry.score for entry in backward]

    def test_tied_items_are_ordered_by_identity(self):
        """RRF 는 동점을 자주 만든다 — 삽입 순서에 기대면 같은 질의가 다른 순서를 낸다."""
        fused = fuse([ranked("dense", item("b-doc")), ranked("lexical", item("a-doc"))])

        assert ids(fused) == ["a-doc", "b-doc"]
        assert fused[0].score == fused[1].score

    def test_ties_within_one_document_fall_back_to_chunk_index(self):
        first = item("doc", chunk_index=1)
        second = item("doc", chunk_index=0)
        fused = fuse([ranked("dense", first), ranked("lexical", second)])

        assert [entry.item.chunk_index for entry in fused] == [0, 1]


class TestScoreIsNormalizedToUnitRange:
    def test_an_item_ranked_first_everywhere_scores_exactly_one(self):
        fused = fuse([ranked("dense", A, B), ranked("lexical", A, C)])

        assert fused[0].score == 1.0

    def test_unanimous_first_place_is_exactly_one_under_mixed_weights(self):
        """`0.9999999999999999` 는 척도의 한쪽 끝이 고정되어 있다는 뜻을 지우지 못한다."""
        fused = fuse(
            [
                ranked("dense", A, weight=1.0),
                ranked("lexical", A, weight=2.0),
                ranked("sparse", A, weight=3.5),
            ]
        )

        assert fused[0].score == 1.0

    def test_delivery_order_matches_to_the_last_bit(self):
        heavy = ranked("dense", A, B, weight=1.0)
        light = ranked("lexical", B, C, weight=2.0)
        third = ranked("sparse", C, A, weight=3.5)

        forward = fuse([heavy, light, third])
        shuffled = fuse([third, heavy, light])

        assert [entry.score for entry in forward] == [entry.score for entry in shuffled]

    def test_an_empty_list_keeps_its_weight_in_the_denominator(self):
        """빈 목록은 "그 retriever 가 아무것도 인정하지 않았다"는 판정이라 척도에 남는다."""
        fused = fuse([ranked("dense", A), ranked("lexical")])

        assert fused[0].score == 0.5

    def test_a_list_that_was_not_passed_is_absent_from_the_denominator(self):
        fused = fuse([ranked("dense", A)])

        assert fused[0].score == 1.0

    def test_every_score_sits_in_the_open_unit_range_and_descends(self):
        fused = fuse([ranked("dense", A, B, C, weight=1.5), ranked("lexical", D, C, weight=0.5)])

        scores = [entry.score for entry in fused]
        assert all(0 < score <= 1 for score in scores)
        assert scores == sorted(scores, reverse=True)


class TestResultCarriesItsProvenance:
    def test_an_item_from_one_list_has_one_contribution(self):
        fused = fuse([ranked("dense", A, item("b", native_score=0.71)), ranked("lexical", C)])
        entry = next(e for e in fused if e.item.document_id == "b")

        assert len(entry.contributions) == 1
        assert entry.contributions[0].retriever == "dense"
        assert entry.contributions[0].rank == 2
        assert entry.contributions[0].native_score == 0.71

    def test_an_item_from_both_lists_has_two_contributions(self):
        fused = fuse(
            [
                ranked("dense", item("a", native_score=0.9), B),
                ranked("lexical", C, item("a", native_score=4.2)),
            ]
        )
        entry = next(e for e in fused if e.item.document_id == "a")
        credits = {c.retriever: (c.rank, c.native_score) for c in entry.contributions}

        assert credits == {"dense": (1, 0.9), "lexical": (2, 4.2)}


class TestDegenerateInputsStillFuse:
    def test_a_single_list_keeps_its_order(self):
        """retriever 하나만 켠 구성이 융합을 우회하지 않고도 이전과 같은 순서를 내야 한다."""
        fused = fuse([ranked("dense", C, A, D, B)])

        assert ids(fused) == ["c", "a", "d", "b"]

    def test_no_lists_at_all_is_empty_and_not_an_error(self):
        assert fuse([]) == []

    def test_only_empty_lists_is_empty_and_not_an_error(self):
        assert fuse([ranked("dense"), ranked("lexical")]) == []

    def test_a_duplicate_within_one_list_counts_once_at_its_best_rank(self):
        fused = fuse([ranked("dense", A, B, A)])
        best_only = fuse([ranked("dense", A, B)])

        assert ids(fused) == ["a", "b"]
        assert len(fused[0].contributions) == 1
        assert [entry.score for entry in fused] == [entry.score for entry in best_only]


class TestTruncationIsTheCallersChoice:
    def test_top_k_cuts_to_the_head_of_the_full_fusion(self):
        many = [item(f"doc-{index:02d}") for index in range(20)]
        full = fuse([ranked("dense", *many)])
        cut = fuse([ranked("dense", *many)], top_k=5)

        assert len(cut) == 5
        assert ids(cut) == ids(full)[:5]

    def test_fewer_candidates_than_k_are_not_padded(self):
        fused = fuse([ranked("dense", A, B)], top_k=5)

        assert len(fused) == 2

    def test_a_list_shorter_than_k_is_a_normal_input(self):
        """하한이 융합 앞에 있어 짧은 목록은 고장이 아니라 정상 경로다."""
        fused = fuse([ranked("dense", A), ranked("lexical", B, C)], top_k=10)

        assert len(fused) == 3


class TestRrfConstant:
    def test_the_constant_shifts_how_steeply_rank_decays(self):
        """상수는 설정 항목이라 값이 바뀌면 점수가 따라 움직여야 한다."""
        flat = fuse([ranked("dense", A, B)], rrf_k=DEFAULT_RRF_K)
        steep = fuse([ranked("dense", A, B)], rrf_k=1)

        assert steep[1].score < flat[1].score
