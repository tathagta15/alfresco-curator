FROM python:3.12-slim

# Uvicorn is the only long-running process, so the image stays minimal and
# runs unprivileged.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY scripts ./scripts

RUN useradd --create-home --uid 10001 curator && chown -R curator:curator /app
USER curator

EXPOSE 8600

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8600/api/health', timeout=3).status_code==200 else 1)"

CMD ["uvicorn", "alfresco_curator.web.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8600"]
