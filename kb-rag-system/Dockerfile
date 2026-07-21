# Runtime image fixed to the reviewed manifest digest in ci/tool-images.env.
FROM python:3.12-slim@sha256:64695412729fbe8cf054511723820c82bbe5a077d4a6b4070cd4a7225d3422ce

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Runtime dependencies are resolved on Linux/Python 3.12 and every artifact is
# hash-verified.  The pip bundled in the pinned base image is intentionally not
# upgraded implicitly.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock

RUN useradd --create-home --uid 1000 appuser

COPY --chown=appuser:appuser data_pipeline/ ./data_pipeline/
COPY --chown=appuser:appuser api/ ./api/
COPY --chown=appuser:appuser ui/ ./ui/

USER 1000
EXPOSE 8000

# Cloud Run ignores this Docker healthcheck, but it remains useful for the
# immutable-image smoke gate without adding curl to the runtime image.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=5)"]

# Shell-free entrypoint: PORT/WEB_CONCURRENCY are parsed as bounded decimal
# integers before execve, so environment values can never become commands.
CMD ["python", "-m", "api.entrypoint"]
