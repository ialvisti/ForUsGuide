# Builder and minimal runtime use the same reviewed Chainguard snapshot from
# ci/tool-images.env, so Python and native-wheel ABIs remain identical.
FROM cgr.dev/chainguard/python:latest-dev@sha256:14acabef9a759e7d07bf647afec92bc28cbe0f89c978fe426411c85035121c14 AS python-runtime

USER 0
WORKDIR /build

# Runtime dependencies are resolved on Linux and every artifact is
# hash-verified.  The pip bundled in the pinned base image is intentionally not
# upgraded implicitly.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --target=/opt/python \
    --require-hashes -r requirements.lock

FROM cgr.dev/chainguard/python:latest@sha256:d812438658b47b73cb4c089f4cca09bca1ba50f6cd1843133864ee074d9ec49b

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
