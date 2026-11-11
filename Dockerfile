# PodWave — app + background job server
# uv-managed image: uv + the latest CPython uv ships a Debian image for (3.15).
# pyproject.toml requires >= 3.14; .python-version pins what the image provides.
FROM ghcr.io/astral-sh/uv:python3.15-trixie-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PODWAVE_DATA=/data \
    PODWAVE_STATIC=/app/public \
    PODWAVE_BIND=0.0.0.0 \
    PODWAVE_PORT=8080

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY server/ ./server/
COPY public/ ./public/

# uv creates the project environment; the app needs no third-party packages.
# Cache goes to /tmp so a read-only root filesystem still works at runtime.
RUN mkdir -p /data && chown python /data \
 && UV_CACHE_DIR=/tmp/uv-cache uv sync --frozen

USER python
VOLUME /data

EXPOSE 8080
# python:3.14+ images are Debian-based and have no wget, so ask with the stdlib
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status==200 else 1)"]
# .venv/bin/python is the uv-created environment; `uv run ...` does the same,
# but needs a writable cache dir at start-up.
CMD ["/app/.venv/bin/python", "/app/server/podwave.py"]
