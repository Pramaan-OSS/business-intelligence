FROM ghcr.io/astral-sh/uv:0.11.26 AS installer
FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates mariadb-client build-essential pkg-config libmariadb-dev libffi-dev chromium fonts-liberation && rm -rf /var/lib/apt/lists/*
COPY --from=installer /uv /uvx /usr/local/bin/
WORKDIR /app
COPY . .
RUN ./review/prepare-local
ENV PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin" FRAPPE_PRELOAD_DATABASE_DRIVERS=none
CMD ["./docker/serve"]
