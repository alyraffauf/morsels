FROM python:3.14-slim

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (layer caching)
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY main.py config.py identity.py atproto_oauth.py schema.sql ./
COPY templates/ templates/

# Create data directory for secrets and database
RUN mkdir -p /data

ENV MORSEL_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uv", "run", "gunicorn", "main:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
