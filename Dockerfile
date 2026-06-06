# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

LABEL org.opencontainers.image.title="github-team-sync" \
      org.opencontainers.image.description="GitHub team sync (HackUCF Keycloak fork)" \
      org.opencontainers.image.source="https://github.com/HackUCF/github-team-sync"

ARG TZ=UTC
ENV TZ=${TZ} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/opt/github-team-sync/.venv/bin:$PATH"

WORKDIR /opt/github-team-sync

# Install dependencies first (cached unless pyproject.toml/uv.lock change).
# Default install = core runtime + Keycloak only; other backends are opt-in extras.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Copy the application source.
COPY . /opt/github-team-sync

CMD ["flask", "run"]
