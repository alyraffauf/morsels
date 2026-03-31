FROM python:3.14-slim

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Tailwind CSS standalone CLI
ADD https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64 /usr/local/bin/tailwindcss
RUN chmod +x /usr/local/bin/tailwindcss

# Copy dependency files first (layer caching)
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY main.py config.py identity.py atproto_oauth.py schema.sql ./
COPY templates/ templates/
COPY static/input.css static/

# Build Tailwind CSS
RUN tailwindcss -i static/input.css -o static/tailwind.css --minify

# Create data directory for secrets and database
RUN mkdir -p /data

ENV MORSEL_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uv", "run", "gunicorn", "main:app", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", "--preload", "--timeout", "120"]
