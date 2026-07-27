"""Shell-free runtime entrypoint for the Cloud Run API image."""

from __future__ import annotations

import os
from collections.abc import Mapping


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(name, str(default))
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be a decimal integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def main(environment: Mapping[str, str] | None = None) -> None:
    """Validate env configuration and replace this process with uvicorn."""
    source = os.environ if environment is None else environment
    port = _bounded_integer(
        source, "PORT", default=8000, minimum=1, maximum=65_535,
    )
    workers = _bounded_integer(
        source, "WEB_CONCURRENCY", default=1, minimum=1, maximum=64,
    )
    argv = [
        "uvicorn",
        "api.main:app",
        "--host", "0.0.0.0",  # noqa: S104 - required by Cloud Run
        "--port", str(port),
        "--workers", str(workers),
    ]
    os.execvp(argv[0], argv)  # noqa: S606 - reviewed argv; no shell involved


if __name__ == "__main__":  # pragma: no cover - process replacement
    main()
