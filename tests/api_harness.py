"""HTTP 경계 테스트의 공통 도구.

업로드 요청 한 건을 만드는 것과, "지금 수집되어 있는 문서가 무엇인가"를 API 로 묻는 것.
둘 다 거의 모든 API 테스트에 나오므로 여기 모아 둔다 — 특히 목록 조회는 **거부된 업로드가
아무 흔적도 남기지 않았는지**를 확인하는 표준 수단이라 형태가 흔들리면 안 된다.
"""

from httpx import AsyncClient

#: 기본 청크 크기(600자)에서 여러 청크로 나뉘는 문서.
LONG_KOREAN = (
    "사내 복리후생 안내\n\n" + "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 합니다. " * 20
)

#: 어떤 구성에서도 청크 하나로 끝나는 문서. 재업로드로 청크 수가 줄어드는지 볼 때 쓴다.
SHORT_KOREAN = "재택근무는 주 2회까지 가능합니다."


def upload(filename: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
    """`client.post("/documents", **upload(...))` 형태로 쓰는 multipart 인자."""
    return {"files": {"file": (filename, data, content_type)}}


async def document_ids(client: AsyncClient) -> list[str]:
    """목록에 있는 `document_id` 들. 응답 순서를 그대로 유지한다."""
    body = (await client.get("/documents")).json()
    return [document["document_id"] for document in body["documents"]]
