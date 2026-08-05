# ── Codex CLI ───────────────────────────────────────────────────────────
#
# Codex SDK 는 HTTP 클라이언트가 아니라 로컬 CLI 를 실행한다. 컨테이너 안에서 답변을
# 생성하려면 파이썬만으로는 부족하고 Node 런타임과 CLI 바이너리가 이미지에 있어야 한다
# (design 결정 5). QA change 에서 베이스 이미지를 다시 짜지 않으려고 지금 넣어둔다 —
# **이 change 는 CLI 를 호출하지 않는다.**
#
# 공식 node 이미지에서 설치하고 결과만 가져온다. 데비안의 `npm` 패키지를 쓰지 않는
# 이유는 그것이 `node-*` 의존성 395개(약 50MB)를 끌고 오기 때문이다 — 실제로 그중 두
# 개가 미러에서 400 을 받아 빌드가 통째로 깨졌다. 평가자 환경에서 같은 식으로 깨지면
# "실행 불가"가 되므로, 미러 상태에 걸린 표면을 아예 없앤다.
FROM node:22-slim AS codex-cli
RUN npm install -g @openai/codex && npm cache clean --force

FROM python:3.11-slim AS runtime

# curl 은 compose 헬스체크가 쓴다. `ca-certificates` 는 선택이 아니다 — 빠뜨리면 codex 가
# 인증까지 통과하고도 모든 호출이 `invalid peer certificate: UnknownIssuer` 로 죽는다.
# 실측으로 확인한 실패 형태이고, 인증 문제로 오인하기 딱 좋은 증상이다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# `codex` 는 네이티브 바이너리를 고르는 node 셤이라 런타임이 함께 있어야 한다.
# 심볼릭 링크는 npm 이 만들던 것과 같은 모양이다.
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && codex --version

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 자격증명은 이미지에 굽지 않는다. 이미지에 넣으면 리포에 남고, 평가자 환경마다 인증
# 주체가 다르다. 호스트의 기존 것(`~/.codex/auth.json`)을 실행 시점에 홈 디렉터리로
# 마운트한다 (결정 5-1). 옮기는 일은 compose 의 `auth` 서비스가 한다.
RUN useradd --create-home --uid 1000 app

# ── 임베딩 런타임 (무거운 레이어 — 코드보다 먼저 굳힌다) ────────────────
#
# torch 를 **CPU 전용 휠**로 받는다. 기본 인덱스의 리눅스 x86_64 휠은 CUDA 런타임을
# 함께 끌고 와 이미지가 수 GB 늘어나는데, 이 서비스는 GPU 를 쓰지 않는다.
#
# `--index-url` 이 아니라 `--extra-index-url` 인 이유: CPU 인덱스에는 aarch64 리눅스
# 휠이 없다. 인덱스를 통째로 갈아끼우면 arm64 빌드(애플 실리콘)가 아예 실패한다.
# 보조 인덱스로 두면 x86_64 에서는 `+cpu` 휠이(로컬 버전이 더 높게 정렬된다) 선택되고,
# arm64 에서는 PyPI 의 기본 휠로 떨어진다 — 어차피 aarch64 에는 CUDA 빌드가 없다.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir sentence-transformers

# ── 모델 가중치 굽기 ────────────────────────────────────────────────────
#
# **`COPY src` 보다 먼저다.** 코드를 고칠 때마다 수백 MB 를 다시 받으면 빌드가 실용적이지
# 않다. 굽지 않으면 기동 시 선로딩이 매번 다운로드를 기다린다 — 런타임 네트워크 의존이
# 생기고, 평가자 환경이 오프라인이면 첫 수집까지 실패한다.
#
# 모델 이름을 환경변수 하나로 두는 이유: 굽는 모델과 런타임이 쓰는 모델이 **구조적으로**
# 같아야 한다. 두 곳에 따로 적으면 어긋난 순간 기동 시 다운로드가 조용히 되살아난다.
#
# 리랭커 리비전만 두 곳에 적힌다 — `Dockerfile` 은 앱 패키지를 import 할 수 없다.
# 어긋나면 런타임이 고정한 커밋을 캐시에서 찾지 못해 다운로드가 조용히 되살아나므로,
# `KNOWN_RERANKER_PROFILES` 와 같은 값인지는 테스트가 이 파일을 읽어 고정한다.
ENV APP_EMBEDDING_MODEL=intfloat/multilingual-e5-small \
    APP_RERANKER_MODEL=BAAI/bge-reranker-v2-m3 \
    APP_RERANKER_REVISION=953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e \
    HF_HOME=/opt/huggingface
RUN python -c "\
import os;\
from huggingface_hub import snapshot_download;\
from sentence_transformers import SentenceTransformer;\
SentenceTransformer(os.environ['APP_EMBEDDING_MODEL']);\
snapshot_download(os.environ['APP_RERANKER_MODEL'],\
 revision=os.environ['APP_RERANKER_REVISION'])" \
    && chown -R app:app /opt/huggingface

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# 문서 레지스트리(SQLite) 경로. 벡터 스토어는 별도 서비스라 여기 없다.
# 볼륨이 붙지 않으면 컨테이너를 다시 만들 때 문서 목록이 사라진다.
RUN mkdir -p /data && chown -R app:app /data /app

USER app
EXPOSE 8000

# compose 가 command 를 덮어쓴다. 여기 기본값은 compose 없이 이미지만 돌릴 때를 위한 것.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]

# ── 테스트 스테이지 ─────────────────────────────────────────────────────
#
# **검색 품질 테스트를 실제로 돌리기 위한 스테이지다.** 그 층은 로컬 임베딩 실물을 쓰는데,
# 호스트에 가중치가 없으면 통째로 건너뛴다(`pytest` 한 줄이 수백 MB 다운로드에 묶이면 안
# 되므로 의도된 동작이다). 이 이미지에는 가중치가 이미 구워져 있어 그 층까지 돈다 —
# 즉 여기서만 "임베딩이 의미를 잡는가"가 실행된다.
#
# 런타임 이미지를 **덧쌓는다.** 별도로 다시 빌드하면 굽는 가중치와 런타임의 가중치가
# 어긋날 수 있는데, 그러면 여기서 잰 품질이 배포되는 구성의 것이 아니게 된다.
#
# 기본 빌드 대상이 되지 않게 `docker-compose.yml` 의 `api` 가 `target: runtime` 을
# 명시한다 — 스테이지가 여럿일 때 `--target` 없는 빌드는 **마지막 스테이지**를 고른다.
FROM runtime AS test

USER root
COPY tests ./tests
COPY sample-docs ./sample-docs
# 자격증명 동기화 스크립트는 api 가 아니라 compose 의 `auth` 서비스가 쓰는 것이라 런타임
# 이미지에 없다. 그래도 여기 넣는 이유는 그 **본문을 읽어 제약을 고정하는** 테스트가
# 있어서다 — 없으면 컨테이너 실행만 그 회귀 방어를 잃는다.
COPY scripts ./scripts
# compose 파일 자체를 읽어 마운트·의존 계약을 단언하는 테스트가 있다. 자격증명이 붙는
# 경로는 파이썬 코드가 아니라 이 YAML 에만 적혀 있어서, 여기가 조용히 바뀌면 다른 어떤
# 테스트도 알아채지 못한다. `Dockerfile` 도 같은 이유다 — 굽는 리랭커 리비전이 여기에만
# 적혀 있고, 어댑터의 표와 어긋나면 런타임에 조용히 다시 받는다.
COPY docker-compose.yml Dockerfile ./
RUN pip install --no-cache-dir ".[dev]"
USER app

CMD ["pytest", "-q"]
