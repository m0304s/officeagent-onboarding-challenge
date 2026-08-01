"""문서 업로드 엔드포인트.

여기서 고정하는 것은 HTTP 경계의 행동이다 — 어떤 요청이 어떤 상태 코드와 어떤 봉투를
받는가. 추출·분할 자체의 성질은 `test_ingestion.py`·`test_chunking.py` 가 덮는다.

> 저장 단계가 아직 없어 응답은 `200` 이고 `index_signature`·`status` 가 없다. 저장이
> 들어오면 이 파일의 기대값이 spec 이 정한 `201` 과 그 필드들로 바뀐다.
"""

import pytest

from app.config import Settings
from tests.pdf_fixtures import BLANK_PAGE, make_pdf

LONG_KOREAN = (
    "사내 복리후생 안내\n\n"
    + "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 합니다. " * 20
)


def upload(filename: str, data: bytes, content_type: str = "application/octet-stream"):
    return {"files": {"file": (filename, data, content_type)}}


# ── 성공 경로 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected_format"),
    [("company-policy.txt", "txt"), ("development-guide.md", "md")],
)
async def test_text_upload_returns_chunks(client, filename, expected_format):
    response = await client.post(
        "/documents", **upload(filename, LONG_KOREAN.encode("utf-8"))
    )

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == expected_format
    assert body["chunk_count"] >= 1
    assert len(body["chunks"]) == body["chunk_count"]
    assert body["page_count"] is None


async def test_pdf_upload_reports_pages_on_every_chunk(client):
    data = make_pdf(["첫째 쪽 본문입니다.", "둘째 쪽 본문입니다."])

    response = await client.post("/documents", **upload("manual.pdf", data))

    body = response.json()
    assert response.status_code == 200
    assert body["page_count"] == 2
    assert {chunk["page"] for chunk in body["chunks"]} == {1, 2}


async def test_response_carries_the_document_identity(client):
    """`document_id` 는 파일명에서, `revision` 은 내용에서 나온다."""
    data = LONG_KOREAN.encode("utf-8")

    first = (await client.post("/documents", **upload("policy.txt", data))).json()
    again = (await client.post("/documents", **upload("policy.txt", data + b"!"))).json()

    assert first["document_id"] == again["document_id"]
    assert first["revision"] != again["revision"]


async def test_chunk_offsets_point_back_into_the_source(client):
    response = await client.post(
        "/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8"))
    )

    for chunk in response.json()["chunks"]:
        assert chunk["char_start"] < chunk["char_end"]
        assert chunk["text"] == LONG_KOREAN[chunk["char_start"] : chunk["char_end"]]


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
async def test_error_paths_map_to_their_status(client, filename, data, status, code):
    response = await client.post("/documents", **upload(filename, data))

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


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


# ── 업로드 상한 ─────────────────────────────────────────────────────────


@pytest.fixture
def small_limit_client(make_client, healthy_probes, settings, monkeypatch):
    """상한을 아주 작게 잡은 클라이언트. 큰 파일을 실제로 만들지 않기 위해."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    tiny = settings.model_copy(update={"max_upload_bytes": 100})
    app = create_app(settings=tiny, probes=healthy_probes)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_uploads_over_the_limit_are_rejected_with_the_limit(small_limit_client):
    async with small_limit_client as client:
        response = await client.post("/documents", **upload("big.txt", b"x" * 500))

    error = response.json()["error"]
    assert response.status_code == 413
    assert error["code"] == "document_too_large"
    assert error["max_upload_bytes"] == 100


async def test_a_file_far_over_the_limit_is_still_rejected(healthy_probes, settings):
    """상한이 막는 것은 수신이 아니라 파싱·임베딩이다.

    `UploadFile` 은 핸들러가 불릴 때 본문이 이미 수신된 뒤다(1 MiB 를 넘으면 디스크로
    스풀된다). 수신 자체를 끊으려면 리버스 프록시의 본문 상한이 앞단에 필요하다.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    tiny = settings.model_copy(update={"max_upload_bytes": 1024})
    app = create_app(settings=tiny, probes=healthy_probes)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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

    assert response.status_code == 200
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

    assert response.status_code == 200


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


async def test_extraction_is_logged_with_structured_fields(client, caplog):
    """무엇이 어떻게 잘렸는지가 로그 한 줄로 남아야 진단이 된다."""
    with caplog.at_level("INFO", logger="app.api.routes.documents"):
        await client.post("/documents", **upload("policy.txt", LONG_KOREAN.encode("utf-8")))

    record = next(r for r in caplog.records if r.message == "문서 추출 완료")
    # `filename` 이 아니다 — `LogRecord` 가 이미 그 이름을 쓴다(로그를 남긴 소스 파일).
    assert record.document_filename == "policy.txt"
    assert record.format == "txt"
    assert record.chunk_count >= 1
    assert record.byte_size > 0


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
    assert record.status_code == 200
    assert record.path == "/documents"


def test_the_upload_limit_has_a_default():
    """설정을 하나도 주지 않아도 상한이 걸려 있어야 한다."""
    assert Settings().max_upload_bytes > 0
