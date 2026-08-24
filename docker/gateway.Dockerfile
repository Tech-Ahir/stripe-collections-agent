# ACTION GATEWAY (container: gateway)
#
# The only component that can act on the outside world. It publishes no port: see
# docker-compose.yml. This image contains no `app/` directory and no Anthropic key --
# the gateway cannot run the agent any more than the agent can run the gateway.

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/srv

WORKDIR /srv

COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# Only the gateway and the shared contract. Deliberately not app/.
COPY shared/ ./shared/
COPY gateway/ ./gateway/

RUN mkdir -p /data /data/outbox

# No EXPOSE, and no ports: mapping in compose. Internal network only.

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=4).status==200 else 1)"]

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "9000"]
