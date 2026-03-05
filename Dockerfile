FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system-level dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    bubblewrap \
    sqlite3 \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies first (layer caching) ───────────────
COPY pyproject.toml ./
RUN pip install --no-cache-dir . \
    && pip cache purge 2>/dev/null || true

# ── Copy application source ─────────────────────────────────────────
COPY src/ src/
COPY .env.example .env.example

# Re-install in editable-ish mode so the package is on sys.path
RUN pip install --no-cache-dir --no-deps .

# Create default data directories
RUN mkdir -p /app/data /app/models

# Run as non-root
RUN groupadd --gid 1000 turing \
    && useradd --uid 1000 --gid turing --create-home turing \
    && chown -R turing:turing /app
USER turing

EXPOSE 5670

CMD ["python", "-m", "turing"]
