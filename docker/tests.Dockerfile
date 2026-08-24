# TEST RUNNER
#
# The only image that contains both services, because the boundary tests must be able to
# import each side in order to prove they stay apart. It is not a runtime service: compose
# puts it behind the `test` profile so `docker compose up` never starts it.
#
#   docker compose run --rm tests                    # the whole suite
#   docker compose run --rm tests pytest -k refusal  # just the boundary refusals

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/srv

WORKDIR /srv

COPY requirements-dev.txt ./
RUN uv pip install --system --no-cache -r requirements-dev.txt

COPY pyproject.toml .importlinter ./
# The deployment configuration is an input to the test suite: tests/test_compose_boundary.py
# asserts on the compose file and the Dockerfiles themselves, because "publish the gateway
# port to make testing easier" is not a bug any functional test would catch.
COPY docker-compose.yml ./
COPY docker/ ./docker/
COPY shared/ ./shared/
COPY app/ ./app/
COPY gateway/ ./gateway/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

RUN mkdir -p /data

CMD ["pytest", "-q"]
