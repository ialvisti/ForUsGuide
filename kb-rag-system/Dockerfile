# Builder and minimal runtime use the same reviewed Chainguard snapshot from
# ci/tool-images.env, so Python and native-wheel ABIs remain identical.
FROM cgr.dev/chainguard/python:latest-dev@sha256:7a568bcee42666f73f041645a41c913ce1d442f4c24cf6019bc543a90820e531 AS python-runtime

USER 0
WORKDIR /build

# Runtime dependencies are resolved on Linux and every artifact is
# hash-verified.  The pip bundled in the pinned base image is intentionally not
# upgraded implicitly.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --target=/opt/python \
    --require-hashes -r requirements.lock

FROM cgr.dev/chainguard/python:latest@sha256:a0365f7b90bf7b78a5e35f2709efb7c9263acf9c7b1905e0ec4c3e943c88e64d

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/python \
    PORT=8000

COPY --from=python-runtime /opt/python /opt/python
COPY --chown=65532:65532 data_pipeline/ ./data_pipeline/
COPY --chown=65532:65532 api/ ./api/
COPY --chown=65532:65532 ui/ ./ui/

USER 65532
EXPOSE 8000

# Cloud Run ignores this Docker healthcheck, but it remains useful for the
# immutable-image smoke gate without adding curl to the runtime image.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=5)"]

# Shell-free entrypoint: PORT/WEB_CONCURRENCY are parsed as bounded decimal
# integers before execve, so environment values can never become commands.
ENTRYPOINT []
CMD ["python", "-m", "api.entrypoint"]
