"""HTTP 경계 테스트의 공통 도구 — 업로드 요청 하나와 현재 문서 목록 조회.

목록 조회는 거부된 업로드가 흔적을 남기지 않았는지 확인하는 표준 수단이라, 형태가
흔들리면 그 단언의 근거가 함께 사라진다.
"""

from httpx import AsyncClient

#: 기본 청크 크기(600자)에서 여러 청크로 나뉘는 문서.
LONG_KOREAN = (
    "사내 복리후생 안내\n\n" + "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 합니다. " * 20
)

#: 어떤 구성에서도 청크 하나로 끝나는 문서. 재업로드로 청크 수가 줄어드는지 볼 때 쓴다.
SHORT_KOREAN = "재택근무는 주 2회까지 가능합니다."


def upload(filename: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
    """업로드 요청 하나의 multipart 인자. `client.post("/documents", ...)` 에 펼쳐 넣는다."""
    return {"files": {"file": (filename, data, content_type)}}


async def document_ids(client: AsyncClient) -> list[str]:
    """목록에 있는 `document_id` 들. 응답 순서를 그대로 유지한다."""
    body = (await client.get("/documents")).json()
    return [document["document_id"] for document in body["documents"]]
