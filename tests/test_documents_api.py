"""문서 업로드 엔드포인트.

여기서 고정하는 것은 **업로드 한 건이 HTTP 경계에서 무엇을 약속하는가**다 — 어떤 요청이
어떤 상태 코드와 어떤 봉투를 받고, 그때 저장소에 실제로 무엇이 남는가. 목록·상세·삭제와
재업로드 의미론은 `test_documents_crud_api.py` 가, 추출·분할 자체의 성질은
`test_ingestion.py`·`test_chunking.py` 가 덮는다.

**응답만 보고 끝내지 않는다.** spec 이 요구하는 것은 "`chunk_count` 만큼의 청크가 벡터
스토어에 실제로 저장되었는가"이지 "응답에 숫자가 적혀 있는가"가 아니다. 그래서 대부분의
단언이 주입된 `vector_store` 대역을 함께 들여다본다.
"""

import pytest

from app.config import Settings
from tests.api_harness import LONG_KOREAN, document_ids, upload
from tests.pdf_fixtures import BLANK_PAGE, make_pdf

# ── 성공 경로 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected_format"),
    [("company-policy.txt", "txt"), ("development-guide.md", "md")],
)
async def test_a_text_document_is_stored_as_chunks(client, vector_store, filename, expected_format):
    response = await client.post("/documents", **upload(filename, LONG_KOREAN.encode("utf-8")))

    body = response.json()
    assert response.status_code == 201
    assert body["format"] == expected_format
    assert body["status"] == "created"
    assert body["index_status"] == "indexed"
    assert body["chunk_count"] >= 1
    assert body["index_signature"]
    assert body["ingested_at"]
    # 응답의 숫자와 실제 저장물이 어긋나면 그 응답은 거짓이다.
    assert await vector_store.count_chunks(body["document_id"]) == body["chunk_count"]


async def test_a_pdf_with_a_text_layer_is_stored_with_its_text(client, vector_store):
    data = make_pdf(["첫째 쪽 본문입니다.", "둘째 쪽 본문입니다."])

    response = await client.post("/documents", **upload("manual.pdf", data))

    body = response.json()
    assert response.status_code == 201
    assert body["format"] == "pdf"
    assert body["chunk_count"] >= 1
    stored = " ".join(chunk.text for chunk in vector_store.chunks_of(body["document_id"]))
    assert "첫째 쪽 본문입니다." in stored


async def test_every_stored_chunk_carries_an_embedding_of_the_same_dimension(
    client, vector_store, embedder
):
    """벡터 없이 저장된 청크는 검색되지 않는다 — 저장됐다는 응답이 거짓이 된다."""
    response = await client.post("/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8")))

    vectors = vector_store.embeddings_of(response.json()["document_id"])
    assert len(vectors) == response.json()["chunk_count"]
    assert {len(vector) for vector in vectors} == {embedder.dimension}


async def test_the_response_carries_the_document_identity(client):
    """`document_id` 는 파일명에서, `revision` 은 내용에서 나온다."""
    data = LONG_KOREAN.encode("utf-8")

    first = (await client.post("/documents", **upload("policy.txt", data))).json()
    again = (await client.post("/documents", **upload("policy.txt", data + b"!"))).json()

    assert first["document_id"] == again["document_id"]
    assert first["revision"] != again["revision"]


async def test_different_filenames_are_different_documents(client):
    data = LONG_KOREAN.encode("utf-8")

    first = (await client.post("/documents", **upload("policy.txt", data))).json()
    second = (await client.post("/documents", **upload("guide.txt", data))).json()

    assert first["document_id"] != second["document_id"]
    assert first["revision"] == second["revision"]
    assert sorted(await document_ids(client)) == sorted(
        [first["document_id"], second["document_id"]]
    )


async def test_filenames_differing_only_in_case_are_the_same_document(client):
    data = LONG_KOREAN.encode("utf-8")

    first = (await client.post("/documents", **upload("Policy.TXT", data))).json()
    again = (await client.post("/documents", **upload("policy.txt", data))).json()

    assert first["document_id"] == again["document_id"]
    assert len(await document_ids(client)) == 1


# ── 위치 정보 — 출처 표기의 근거 ────────────────────────────────────────


async def test_pdf_chunks_carry_a_page_number_within_the_document(client, vector_store):
    pages = ["첫째 쪽 본문입니다.", "둘째 쪽 본문입니다.", "셋째 쪽 본문입니다."]

    response = await client.post("/documents", **upload("manual.pdf", make_pdf(pages)))

    chunks = vector_store.chunks_of(response.json()["document_id"])
    assert chunks
    assert all(1 <= chunk.location.page <= len(pages) for chunk in chunks)
    assert len({chunk.location.page for chunk in chunks}) == len(pages)


async def test_text_chunks_carry_offsets_that_point_back_into_the_source(client, vector_store):
    response = await client.post("/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8")))

    for chunk in vector_store.chunks_of(response.json()["document_id"]):
        assert 0 <= chunk.location.char_start < chunk.location.char_end <= len(LONG_KOREAN)
        assert chunk.location.page is None


async def test_stored_chunk_text_is_a_substring_of_the_source(client, vector_store):
    """원문에 없는 내용이 청크에 들어가면 출처 표기가 근거를 잃는다."""
    response = await client.post("/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8")))

    for chunk in vector_store.chunks_of(response.json()["document_id"]):
        assert chunk.text in LONG_KOREAN
        assert chunk.text == LONG_KOREAN[chunk.location.char_start : chunk.location.char_end]


async def test_only_pages_with_text_produce_chunks(client, vector_store):
    """일부 쪽에만 텍스트가 있는 PDF 는 거부가 아니라 수집이다."""
    data = make_pdf([BLANK_PAGE, "본문이 있는 쪽입니다.", BLANK_PAGE])

    response = await client.post("/documents", **upload("partial.pdf", data))

    body = response.json()
    assert response.status_code == 201
    chunks = vector_store.chunks_of(body["document_id"])
    assert {chunk.location.page for chunk in chunks} == {2}
    assert len(chunks) == body["chunk_count"]


# ── 배치 경계 ───────────────────────────────────────────────────────────


async def test_a_document_with_more_chunks_than_the_batch_size_is_stored_whole(
    make_client, settings, vector_store, embedder
):
    """배치 경계에서 청크가 누락되거나 중복되면 순번에 구멍이나 겹침으로 드러난다."""
    tiny_batches = settings.model_copy(
        update={"embedding_batch_size": 2, "chunk_size": 100, "chunk_overlap": 20}
    )

    async with make_client(settings=tiny_batches) as client:
        response = await client.post(
            "/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8"))
        )

    body = response.json()
    assert response.status_code == 201
    assert body["chunk_count"] > 2, "배치가 하나로 끝나면 이 테스트는 아무것도 확인하지 않는다"

    chunks = vector_store.chunks_of(body["document_id"])
    assert [chunk.chunk_index for chunk in chunks] == list(range(body["chunk_count"]))
    assert len(embedder.batches) > 1
    assert max(len(batch) for batch in embedder.batches) == 2


# ── 오류 경로 — 상태 코드 표가 실제로 걸리는지 ──────────────────────────


@pytest.mark.parametrize(
    ("filename", "data", "status", "code"),
    [
        ("report.docx", b"x" * 50, 415, "unsupported_document_format"),
        ("README", b"x" * 50, 415, "unsupported_document_format"),
        ("empty.txt", b"", 422, "empty_document"),
        ("blank.txt", b"   \n\n  ", 422, "empty_document"),
        ("broken.pdf", b"%PDF-1.7 not really", 422, "document_parse_error"),
    ],
)
async def test_rejected_uploads_leave_nothing_behind(
    client, vector_store, filename, data, status, code
):
    before = await document_ids(client)

    response = await client.post("/documents", **upload(filename, data))

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert await document_ids(client) == before
    assert await vector_store.count_chunks() == 0


async def test_a_scanned_pdf_is_not_reported_as_an_empty_document(client):
    """두 코드를 뭉개면 클라이언트가 OCR 필요 여부를 구분할 수 없다."""
    response = await client.post(
        "/documents", **upload("scanned.pdf", make_pdf([BLANK_PAGE, BLANK_PAGE]))
    )

    error = response.json()["error"]
    assert response.status_code == 422
    assert error["code"] == "no_extractable_text"
    assert error["page_count"] == 2
    assert "OCR" in error["message"]


async def test_the_unsupported_format_error_lists_supported_formats(client):
    """소비자가 메시지 문자열을 파싱하지 않고 읽을 수 있어야 한다."""
    response = await client.post("/documents", **upload("report.docx", b"x"))

    error = response.json()["error"]
    assert sorted(error["supported_formats"]) == ["md", "pdf", "txt"]
    # 중첩 `details` 객체를 만들지 않는다 — 봉투 안 형식은 하나여야 한다.
    assert "details" not in error


async def test_parse_errors_do_not_leak_internals(client):
    """응답 본문에 파서의 내부 예외 메시지나 스택 트레이스가 실리면 안 된다."""
    response = await client.post("/documents", **upload("broken.pdf", b"%PDF-1.7 broken"))

    body = response.text
    assert "Traceback" not in body
    assert "mupdf" not in body.lower()


async def test_an_empty_upload_does_not_touch_the_existing_document(client, vector_store):
    """실패한 업로드가 이미 수집된 문서를 건드리면, 잘못된 파일 하나가 멀쩡한 문서를 지운다."""
    first = (
        await client.post("/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8")))
    ).json()

    response = await client.post("/documents", **upload("policy.txt", b""))

    assert response.status_code == 422
    detail = (await client.get(f"/documents/{first['document_id']}")).json()
    assert detail["revision"] == first["revision"]
    assert detail["chunk_count"] == first["chunk_count"]
    assert await vector_store.count_chunks(first["document_id"]) == first["chunk_count"]


async def test_a_storage_failure_is_reported_as_service_unavailable(make_client, vector_store):
    """저장이 실패했는데 2xx 로 답하면 그 응답이 거짓이 된다."""
    vector_store.fail_add_after = 0

    async with make_client() as client:
        response = await client.post(
            "/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8"))
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert await document_ids(client) == []


# ── 업로드 상한 ─────────────────────────────────────────────────────────


@pytest.fixture
def small_limit_client(make_client, settings):
    """상한을 아주 작게 잡은 클라이언트. 큰 파일을 실제로 만들지 않기 위해."""
    return make_client(settings=settings.model_copy(update={"max_upload_bytes": 100}))


async def test_uploads_over_the_limit_are_rejected_with_the_limit(small_limit_client):
    async with small_limit_client as client:
        before = await document_ids(client)
        response = await client.post("/documents", **upload("big.txt", b"x" * 500))
        after = await document_ids(client)

    error = response.json()["error"]
    assert response.status_code == 413
    assert error["code"] == "document_too_large"
    assert error["max_upload_bytes"] == 100
    assert after == before


async def test_a_file_far_over_the_limit_is_still_rejected(make_client, settings):
    """상한이 막는 것은 수신이 아니라 파싱·임베딩이다.

    `UploadFile` 은 핸들러가 불릴 때 본문이 이미 수신된 뒤다(1 MiB 를 넘으면 디스크로
    스풀된다). 수신 자체를 끊으려면 리버스 프록시의 본문 상한이 앞단에 필요하다.
    """
    tiny = settings.model_copy(update={"max_upload_bytes": 1024})

    async with make_client(settings=tiny) as client:
        response = await client.post("/documents", **upload("huge.txt", b"x" * 200_000))

    assert response.status_code == 413
    assert response.json()["error"]["max_upload_bytes"] == 1024


async def test_a_file_larger_than_the_frameworks_part_default_is_accepted(client):
    """프레임워크의 multipart 파트 기본 상한은 1 MiB 다.

    그 기본값이 파일 파트에도 걸린다면, 설정을 20 MiB 로 올려도 2 MiB 파일이 설정과
    무관한 이유로 거절된다. 상한이 하나여야 하므로 이 경계를 고정한다.
    """
    unit = ("사내 복리후생 안내입니다. " * 8 + "\n\n").encode()  # 문자 경계에서 자르지 않는다
    data = unit * (2_000_000 // len(unit))
    assert len(data) > 1024 * 1024

    response = await client.post("/documents", **upload("big.txt", data))

    assert response.status_code == 201
    assert response.json()["chunk_count"] > 1


async def test_the_limit_applies_before_parsing(small_limit_client, monkeypatch):
    """상한 초과는 파싱 이전에 판정되어야 한다 — 큰 파일이 CPU 를 쓰면 상한이 무의미하다."""
    calls = []

    from app.adapters.parsers.text import TextParser

    original = TextParser.parse
    monkeypatch.setattr(
        TextParser,
        "parse",
        lambda self, data: (calls.append(len(data)), original(self, data))[1],
    )

    async with small_limit_client as client:
        await client.post("/documents", **upload("big.txt", b"x" * 500))

    assert calls == [], "상한을 넘은 업로드가 파서까지 도달했다"


async def test_a_file_within_the_limit_passes_even_though_the_body_is_larger(
    small_limit_client,
):
    """상한은 **파일** 크기에 대한 약속이다.

    multipart 봉투(경계선·파트 헤더)까지 세면 상한에 꼭 맞는 파일이 몇백 바이트 때문에
    거절되어, 응답이 알려주는 상한과 실제 동작이 어긋난다. 이 요청의 본문은 상한(100)을
    넘지만 파일은 넘지 않으므로 통과해야 한다.
    """
    async with small_limit_client as client:
        response = await client.post("/documents", **upload("ok.txt", "짧은 본문".encode()))

    assert response.status_code == 201


# ── 요청 형식 ───────────────────────────────────────────────────────────


async def test_a_non_multipart_body_is_rejected(client):
    response = await client.post("/documents", json={"file": "policy.txt"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_multipart_body_without_the_file_part_is_rejected(client):
    response = await client.post("/documents", data={"other": "value"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ── 로깅 ────────────────────────────────────────────────────────────────


async def test_uploads_are_logged_with_structured_fields(client, caplog):
    """무엇이 어떻게 저장됐는지가 로그 한 줄로 남아야 진단이 된다."""
    with caplog.at_level("INFO", logger="app.api.routes.documents"):
        await client.post("/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8")))

    record = next(r for r in caplog.records if r.message == "문서 업로드 완료")
    # `filename` 이 아니다 — `LogRecord` 가 이미 그 이름을 쓴다(로그를 남긴 소스 파일).
    assert record.document_filename == "policy.txt"
    assert record.format == "txt"
    assert record.chunk_count >= 1
    assert record.byte_size > 0
    assert record.ingestion_status == "created"


async def test_document_content_is_not_logged(client, caplog):
    """문서 내용이 로그로 새어 나가면 그 자체가 유출이다."""
    secret = "대외비 급여 테이블 " * 40

    with caplog.at_level("INFO"):
        await client.post("/documents", **upload("policy.txt", secret.encode("utf-8")))

    assert all("대외비" not in str(record.__dict__) for record in caplog.records)


async def test_the_access_log_carries_the_request_id(client, caplog):
    with caplog.at_level("INFO", logger="app.access"):
        response = await client.post(
            "/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8"))
        )

    record = next(r for r in caplog.records if r.message == "요청 처리 완료")
    assert record.request_id == response.headers["x-request-id"]
    assert record.status_code == 201
    assert record.path == "/documents"


def test_the_upload_limit_has_a_default():
    """설정을 하나도 주지 않아도 상한이 걸려 있어야 한다."""
    assert Settings().max_upload_bytes > 0


# ── 실제 저장소 배선 ────────────────────────────────────────────────────


async def test_the_default_wiring_works_with_the_real_stores(settings, healthy_probes, embedder):
    """대역이 아니라 **실제 Chroma·SQLite** 로 배선한 앱에 한 번은 요청해 본다.

    나머지 API 테스트는 전부 저장소 대역을 쓴다 — 저장 결과를 들여다보고 실패를 주입해야
    하기 때문이다. 그래서 "설정의 경로로 실제 어댑터가 만들어지고 그 위에서 수집·조회·
    삭제가 성립하는가"를 확인하는 곳은 여기뿐이다. 이게 없으면 배선이 깨져도 스위트는
    끝까지 초록이고, 그 사실은 평가자가 서비스를 띄웠을 때 드러난다.

    임베더만 대역으로 남긴다. 실제 모델을 쓰면 이 테스트 하나가 수백 MB 다운로드에 묶여
    "구독·네트워크 없이 한 줄 실행"이 깨진다.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app(settings=settings, probes=healthy_probes, embedder=embedder)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = (
            await client.post("/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8")))
        ).json()
        detail = await client.get(f"/documents/{created['document_id']}")
        listing = (await client.get("/documents")).json()
        removed = await client.delete(f"/documents/{created['document_id']}")
        after = await document_ids(client)

    assert created["chunk_count"] >= 1
    assert detail.status_code == 200
    assert detail.json()["chunk_count"] == created["chunk_count"]
    assert listing["count"] == 1
    assert removed.status_code == 204
    assert after == []
    # 파일이 설정된 경로에 실제로 생겼는지 — 경로 배선이 어긋나면 컨테이너에서만 드러난다.
    assert settings.registry_path.exists()
    assert settings.vector_store_path.exists()
