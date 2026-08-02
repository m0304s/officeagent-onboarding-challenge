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
