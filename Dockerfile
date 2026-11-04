FROM ghcr.io/astral-sh/uv:0.8.3 AS uv

FROM python:3.12-slim-bookworm AS builder

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml README.md uv.lock ./
COPY src/ ./src/
COPY langchain-agent/pyproject.toml langchain-agent/uv.lock ./langchain-agent/
COPY langchain-agent/src/ ./langchain-agent/src/
COPY langchain-agent/chainlit_app.py ./langchain-agent/chainlit_app.py
COPY langchain-agent/chainlit.md ./langchain-agent/chainlit.md

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project langchain-agent --extra webhook --frozen --no-dev

COPY scripts/ /app/scripts/

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/lla

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 lla \
    && useradd --uid 10001 --gid lla --create-home --shell /usr/sbin/nologin lla \
    && mkdir -p /app/data \
    && chown -R lla:lla /app/data /home/lla

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

RUN chown -R root:root /app \
    && mkdir -p /app/langchain-agent/.files /app/langchain-agent/.chainlit \
    && chown lla:lla /app/data /app/langchain-agent/.files /app/langchain-agent/.chainlit

WORKDIR /app/langchain-agent
USER lla:lla

EXPOSE 8000 8001
