# 미검증 가정 (스파이크 목록)

구현은 했지만 **실제로 동작하는지 확인하지 않은** 것들을 모아둔 곳입니다. 적어두지 않으면 조용히 사라지고, 마감에 가까워서 발견하면 되돌릴 시간이 없습니다.

각 항목은 그것을 처음 필요로 하는 change의 **첫 태스크**로 옮겨집니다. 확인이 끝나면 결과(사실이었는지, 아니었는지)를 적고 여기서 지웁니다.

## ~~S-1. 컨테이너 안에서 LLM CLI가 실제로 답변을 생성하는가~~ (해소)

- **결과**: **사실이었다.** 리눅스 컨테이너에서 `codex exec`가 실제 답변을 생성하고 토큰 사용량까지 회수했다. `codex exec --json`은 JSONL 이벤트(`thread.started` → `turn.started` → `item.completed` → `turn.completed`)를 뱉는다 — **토큰 델타는 없고 메시지가 통째로 한 이벤트로 온다.** 이 입자도는 QA change의 SSE 설계에 그대로 영향을 준다.
- **부수 발견**: 이미지에 `ca-certificates`가 없으면 **인증까지 통과하고도** 모든 호출이 `invalid peer certificate: UnknownIssuer`로 죽는다. 인증 문제로 오인하기 쉬운 증상이라 `Dockerfile`에 이유와 함께 고정해 두었다.
- **이 확인이 SDK 선택을 뒤집었다**: 원래 `claude-code-sdk`였고, 여기서 "macOS 자격증명이 Keychain이라 컨테이너가 읽을 수 없다"는 벽에 부딪혀 Codex로 갈아탔다. 경위는 `ARCHITECTURE.md`의 "`claude-code-sdk`에서 Codex SDK로 갈아탄 이유"에 있다.

## ~~S-2. 마운트한 자격증명을 컨테이너 안의 CLI가 인증에 쓰는가~~ (해소)

- **결과**: **사실이었다.** 호스트의 `~/.codex/auth.json`을 리눅스 컨테이너에 마운트한 상태에서 `codex login status`가 `Logged in using ChatGPT`를 반환했고, 이어진 `codex exec` 호출이 인증 오류 없이 통과했다.
- **남은 조각**: 위 확인은 `node:22-slim`에 `/root/.codex`로 붙여서 했다. 이 리포의 이미지에서 `app`(uid 1000)이 `/home/app/.codex`로 읽는 경로는 `docker compose up` 실행으로 함께 확인한다.

## S-3. refresh token이 회전해 호스트 CLI가 로그아웃되는가

- **담당 change**: QA
- **현재 상태**: 컨테이너가 가진 것은 **사본**이다(design 결정 5-1). 갱신 시 refresh token이 회전하는 방식이면 호스트가 들고 있는 값이 무효가 되어 호스트 CLI가 로그아웃될 수 있다. **회전 여부 미확인.**
- **확인 방법**: 컨테이너를 access token 만료를 넘겨 띄워 둔 뒤, 사본의 토큰이 바뀌었는지와 호스트 `codex`가 여전히 동작하는지 확인.
- **완화책(이미 적용)**: 기동할 때마다 재추출해 사본이 묵는 창을 컨테이너 수명으로 제한.
- **틀렸을 경우**: 호스트 재로그인이 필요하다는 경고를 `README.md`에 이미 적어 둠. 빈발하면 자격증명을 읽기 전용으로 붙이고 짧은 세션만 지원하는 쪽으로 후퇴.

## S-4. 호스트 UID가 1000이 아닌 리눅스에서 자격증명 마운트가 읽히는가

- **담당 change**: QA 또는 배포 정리
- **현재 상태**: Windows·macOS(Docker Desktop)에서만 확인. Docker Desktop은 바인드 마운트 소유권을 컨테이너 사용자로 매핑해 주지만, 리눅스는 호스트 UID가 그대로 보인다. 자격증명 파일이 0600이라 UID가 다르면 **읽지 못한다.**
- **완화책(이미 적용)**: `auth` 서비스가 root로 돌면서 사본과 그 디렉터리를 `chown 1000:1000` 한다. 컨테이너가 만든 마운트 지점이 root 소유로 남아 api가 갱신에 실패하는 경로를 막는다. **리눅스에서 실제로 그렇게 되는지는 미확인.**
- **확인 방법**: 리눅스 호스트(UID≠1000)에서 `docker compose up` 후 컨테이너 안에서 파일 읽기·쓰기.
- **틀렸을 경우**: `docker-compose.yml`의 `api`에 `user: "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}"` 를 추가하는 안내가 `README.md`에 있다.
