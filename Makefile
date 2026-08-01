.PHONY: up down logs sync-credentials

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
