FROM python:3.11-slim

# `claude-code-sdk` 는 HTTP 클라이언트가 아니라 로컬 CLI 를 실행한다. 컨테이너 안에서
# 답변을 생성하려면 파이썬만으로는 부족하고 Node 런타임과 CLI 바이너리가 이미지에
# 있어야 한다 (design 결정 5). QA change 에서 베이스 이미지를 다시 짜지 않으려고 지금
# 넣어둔다 — **이 change 는 CLI 를 호출하지 않는다.**
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm curl \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 자격증명은 이미지에 굽지 않는다. 이미지에 넣으면 리포에 남고, 평가자 환경마다 인증
# 주체가 다르다. 호스트의 기존 것을 실행 시점에 홈 디렉터리로 마운트한다 (결정 5-1).
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
ENV APP_EMBEDDING_MODEL=intfloat/multilingual-e5-small \
    HF_HOME=/opt/huggingface
RUN python -c "\
import os;\
from sentence_transformers import SentenceTransformer;\
SentenceTransformer(os.environ['APP_EMBEDDING_MODEL'])" \
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
