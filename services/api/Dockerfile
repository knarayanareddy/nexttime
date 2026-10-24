FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.5

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages ./packages
COPY services ./services
COPY content ./content
COPY examples ./examples
COPY schemas ./schemas

RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "nexttime_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
