"""어댑터 계약.

프로토콜은 **실제 소비자가 생기는 change에서** 정의한다. 소비자 없이 미리 정한 인터페이스는
사용처가 생기는 순간 거의 반드시 틀린 것으로 드러나기 때문이다. 그래서 여기 있는 것은
지금까지 실제로 쓰이는 계약뿐이고, `CacheStore`(get/set/invalidate) 처럼 아직 소비자가
없는 인터페이스는 정의하지 않는다.

프로토콜이 주고받는 **값 객체는 `core/`에 둔다.** `HealthProbe`(adapters) ↔ `ProbeResult`
(core)와 같은 배치다. 청킹이 `TextSegment`를 소비하는데 `core/`가 `adapters/`를 import
하면 계층이 역전된다.
"""

from typing import Protocol, runtime_checkable

from app.core.documents import DocumentFormat, ExtractedDocument
from app.core.models import ProbeResult


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
