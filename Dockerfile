# --- Build stage ---
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build deps for any C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only dependency manifest first for cache-friendly layer
COPY pyproject.toml ./

# Install deps into a venv we can copy later
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip && \
    pip install ".[postgres]"

# --- Runtime stage ---
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Minimal runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy application code
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY pyproject.toml ./

# Persistent data dir for SQLite
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Default env (overridable by compose / docker run -e)
ENV LOG_LEVEL=INFO \
    DATABASE_URL=sqlite+aiosqlite:////app/data/groupmate.db

# Drop privileges — never run as root
RUN useradd --create-home --uid 1000 groupmate && \
    chown -R groupmate:groupmate /app
USER groupmate

# Run migrations on startup, then start the bot
CMD ["sh", "-c", "alembic upgrade head && python -m app.main"]
