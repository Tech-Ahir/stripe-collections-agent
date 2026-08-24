# AGENT SERVICE (container: app)
#
# Note what this image does NOT contain: there is no `gateway/` directory in it. The
# import boundary of CLAUDE.md rule 3 is enforced statically by import-linter, at runtime
# by app/guards.py, and here by the filesystem -- the agent's container has no copy of
# the code that can act on the outside world, and no credential to run it with.

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/srv

WORKDIR /srv

# Dependencies first, from a fully pinned lock file, so a rebuild is cache-warm and a
# clean-machine build resolves to exactly the versions this was tested against.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# Only the agent service and the shared contract. Deliberately not gateway/.
COPY shared/ ./shared/
COPY app/ ./app/

RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
