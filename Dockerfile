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
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies first (layer caching) ───────────────
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir . \
    && pip cache purge 2>/dev/null || true

# ── Copy application source ─────────────────────────────────────────
COPY src/ src/
COPY .env.example .env.example

# Re-install in editable-ish mode so the package is on sys.path
RUN pip install --no-cache-dir --no-deps .

# ── Build sqlite-vec from source for correct architecture ──────────
RUN git clone --depth 1 --branch v0.1.6 https://github.com/asg017/sqlite-vec.git /tmp/sqlite-vec \
    && cd /tmp/sqlite-vec \
    && VERSION=$(cat VERSION) \
    && sed -e "s/\${VERSION}/$VERSION/g" \
           -e "s/\${DATE}/$(date +%F)/g" \
           -e "s/\${SOURCE}/v$VERSION/g" \
           -e "s/\${VERSION_MAJOR}/$(echo $VERSION | cut -d. -f1)/g" \
           -e "s/\${VERSION_MINOR}/$(echo $VERSION | cut -d. -f2)/g" \
           -e "s/\${VERSION_PATCH}/$(echo $VERSION | cut -d. -f3)/g" \
           sqlite-vec.h.tmpl > sqlite-vec.h \
    && gcc -O2 -shared -fPIC -o vec0.so sqlite-vec.c -lm \
    && VEC_DIR=$(python3 -c "import sqlite_vec, os; print(os.path.dirname(sqlite_vec.__file__))") \
    && cp vec0.so "$VEC_DIR/vec0.so" \
    && rm -rf /tmp/sqlite-vec

# Create default data directories
RUN mkdir -p /app/data /app/models

# Run as non-root
RUN groupadd --gid 1000 turing \
    && useradd --uid 1000 --gid turing --create-home turing \
    && chown -R turing:turing /app
USER turing

EXPOSE 5670

CMD ["python", "-m", "turing"]
