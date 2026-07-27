# Builder and distroless runtime are fixed to reviewed manifest digests in
# ci/tool-images.env. Debian 12 is used in both stages so copied Python native
# extensions link against the same runtime ABI.
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS python-runtime

WORKDIR /build

# Runtime dependencies are resolved on Linux/Python 3.12 and every artifact is
# hash-verified.  The pip bundled in the pinned base image is intentionally not
# upgraded implicitly.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock

FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

COPY --from=python-runtime /usr/local /usr/local
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
