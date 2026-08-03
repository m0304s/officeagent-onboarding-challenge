#!/bin/sh
#
# 호스트에 **이미 있는** Codex 자격증명을 api 컨테이너가 마운트할 자리로 옮긴다.
#
# `docker compose up` 이 `auth` 서비스로 이걸 먼저 돌리고, 끝난 뒤에야 api 가 뜬다.
# **사용자가 따로 실행하지 않는다.** 호스트에서 도는 스크립트가 아니라 컨테이너 안에서
# 도는 스크립트라 make 도 bash 도 필요 없고, 윈도우·macOS·리눅스가 같은 경로를 탄다.
#
# 새 토큰을 발급하지 않는다 — `codex login` 은 여기서 쓰지 않는다. 호스트의 인증 상태를
# 재사용하는 것이 전부다.
#
# 마운트 계약 (docker-compose.yml 의 `auth` 서비스):
#   /host-codex  ro  호스트의 ~/.codex (윈도우는 %USERPROFILE%\.codex)
#   /secrets     rw  리포의 ./.secrets/codex — api 가 /home/app/.codex 로 마운트한다
#
# **0 이 아닌 코드로 끝나지 않는다.** api 가 `service_completed_successfully` 로 이 서비스에
# 의존하므로, 여기서 실패하면 자격증명이 없는 평가자 환경에서 서비스가 아예 뜨지 않는다.
# 인증 부재는 기동 조건이 아니다 (specs "서비스는 외부 LLM 제공자 인증 없이 기동한다").
set -u

HOST_AUTH=/host-codex/auth.json
SECRETS_DIR=/secrets
TARGET="$SECRETS_DIR/auth.json"

# api 컨테이너는 uid 1000(`app`)으로 돈다. CLI 가 만료된 토큰을 갱신하려면 이 파일에
# **쓸 수** 있어야 한다. 리눅스 호스트에서는 compose 가 만든 마운트 지점이 root 소유라
# 그대로 두면 갱신이 실패한다 — root 인 지금 넘겨준다.
APP_UID=1000
APP_GID=1000

say() { printf '자격증명 동기화: %s\n' "$1"; }

hand_over() {
    chmod 600 "$TARGET" 2>/dev/null || true
    chown "$APP_UID:$APP_GID" "$TARGET" 2>/dev/null || true
}

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR" 2>/dev/null || true
chown "$APP_UID:$APP_GID" "$SECRETS_DIR" 2>/dev/null || true

if [ -r "$HOST_AUTH" ]; then
    # 기동할 때마다 다시 꺼내온다. 컨테이너가 항상 호스트의 최신 상태로 시작하므로 사본이
    # 묵는 창이 컨테이너 수명으로 제한된다.
    if cat "$HOST_AUTH" >"$TARGET" 2>/dev/null; then
        hand_over
        say "호스트(~/.codex/auth.json)의 기존 자격증명을 동기화했습니다."
        exit 0
    fi
    say "호스트 자격증명을 읽었지만 사본을 쓰지 못했습니다."
fi

# 호스트에 파일이 없다고 해서 사본을 지우면 안 된다. 호스트에서 로그아웃했거나 홈 경로가
# 잡히지 않은 경우, 직전까지 쓰던 사본으로 계속 도는 편이 낫다.
if [ -f "$TARGET" ]; then
    hand_over
    say ".secrets/codex 에 있던 기존 사본을 그대로 사용합니다."
    exit 0
fi

say "호스트에서 기존 자격증명을 찾지 못했습니다 (~/.codex/auth.json)."
say "호스트에서 codex 로그인을 마친 뒤 다시 올리면 자동으로 붙습니다."
say "서비스는 그대로 기동됩니다. LLM 기능만 사용할 수 없습니다."
exit 0
