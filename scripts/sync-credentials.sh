#!/usr/bin/env bash
#
# 호스트에 **이미 있는** CLI 자격증명을 컨테이너가 마운트할 위치로 꺼내온다.
#
# 새 토큰을 발급하지 않는다. `claude setup-token`·`claude login` 은 여기서 쓰지 않는다
# (design 결정 5-1의 제약). 호스트의 인증 상태를 재사용하는 것이 전부다.
#
# 자격증명을 찾지 못해도 실패로 끝내지 않는다. 인증 부재가 기동을 막으면 안 되기 때문이다
# (specs "서비스는 외부 LLM 제공자 인증 없이 기동한다").
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 기본값이 compose 가 마운트하는 경로다. 테스트가 실제 자격증명을 건드리지 않고 이 스크립트를
# 돌려볼 수 있도록 override 만 허용한다.
SECRETS_DIR="${SECRETS_DIR:-$REPO_ROOT/.secrets/claude}"
TARGET="$SECRETS_DIR/.credentials.json"
KEYCHAIN_SERVICE="Claude Code-credentials"

say() { printf '자격증명 동기화: %s\n' "$1" >&2; }

# 컨테이너가 마운트할 디렉터리는 자격증명이 없어도 있어야 한다. 없는 상태로 compose 가
# 붙이면 도커가 대신 디렉터리를 만들고, 그 소유자가 root 가 된다.
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

blob=""
case "$(uname -s)" in
  Darwin)
    # macOS 는 자격증명이 Keychain 항목이라 파일이 아니다. 볼륨으로 붙일 수 없어 꺼내온다.
    # 블롭 구조가 Linux 가 읽는 .credentials.json 과 동일해서 그대로 쓸 수 있다.
    blob="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
    ;;
  *)
    # Linux 는 처음부터 파일이다. 그대로 복사한다.
    source_file="${CLAUDE_CREDENTIALS_PATH:-$HOME/.claude/.credentials.json}"
    if [ -r "$source_file" ]; then
      blob="$(cat "$source_file")"
    fi
    ;;
esac

if [ -z "$blob" ]; then
  say "호스트에서 기존 자격증명을 찾지 못했습니다."
  say "서비스는 그대로 기동됩니다. LLM 기능만 사용할 수 없습니다."
  exit 0
fi

umask 077
printf '%s' "$blob" >"$TARGET"
chmod 600 "$TARGET"
say "호스트의 기존 자격증명을 .secrets/claude/.credentials.json 으로 동기화했습니다."
