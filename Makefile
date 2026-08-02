.PHONY: up down logs sync-credentials vector-store test test-all lint

VENV := .venv

# ── 실행 ────────────────────────────────────────────────────────────────
# 평가자가 실행하는 단 하나의 명령.
# `docker compose up` 을 직접 쓰면 자격증명 동기화가 생략된다.
up: sync-credentials
	docker compose up --build

sync-credentials:
	@bash scripts/sync-credentials.sh

down:
	docker compose down

logs:
	docker compose logs -f api

# ── 테스트 ──────────────────────────────────────────────────────────────
# 깨끗한 체크아웃에서 이 한 줄로 끝나야 한다. 가상환경이 없으면 만들고 나서 돈다.
test: $(VENV)/bin/pytest
	$(VENV)/bin/pytest

# 벡터 스토어만 띄운다. Chroma 를 서버 모드로 쓰므로 실물 어댑터 테스트에는 서버가
# 필요하다 — 없으면 그 테스트들은 **건너뛴다**(`make test` 는 그대로 초록).
# 건너뛴 것까지 돌리려면 이쪽을 쓴다.
vector-store:
	docker compose up -d --wait vector-store

test-all: vector-store $(VENV)/bin/pytest
	$(VENV)/bin/pytest

lint: $(VENV)/bin/pytest
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

# 3.11+ 이 필요한데 시스템 `python3` 가 그보다 낮은 맥이 흔하다. 후보를 훑어서 쓸 수
# 있는 것을 고르고, 없으면 조용히 낮은 버전으로 돌지 않고 멈춘다.
$(VENV)/bin/pytest:
	@python=""; \
	for p in python3.14 python3.13 python3.12 python3.11 python3; do \
	  if command -v $$p >/dev/null 2>&1 && \
	     $$p -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then \
	    python=$$p; break; \
	  fi; \
	done; \
	if [ -z "$$python" ]; then \
	  echo "Python 3.11 이상을 찾지 못했습니다. 설치 후 다시 실행하세요."; exit 1; \
	fi; \
	echo "가상환경 생성 ($$python)"; \
	$$python -m venv $(VENV); \
	$(VENV)/bin/pip install -q -e ".[dev]"
