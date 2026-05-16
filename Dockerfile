# ── Stage 1: build the webui SPA bundle ─────────────────────────────
# pyproject.toml force-includes webui/dist into the wheel, so we must
# produce it before the python install step.
FROM node:20-slim AS webui-builder
WORKDIR /webui
COPY webui/package.json webui/package-lock.json* ./
# Drop the committed lockfile inside the builder so npm can resolve the
# platform-specific optional native deps for the build image's arch.
# Works around npm/cli#4828: a package-lock.json captured on one platform
# omits optional @rollup/rollup-<os>-<arch>-* entries needed on another,
# causing `Cannot find module @rollup/rollup-linux-{arm64,x64}-gnu` at
# `npm run build`. We accept slightly non-deterministic webui builds in
# exchange for a simple fix; the webui is not a security-critical surface.
# --legacy-peer-deps because @vitejs/plugin-react's published peer range
# lags behind the vite 8 we use in devDependencies; the build itself
# works fine.
#
# NOTE: `--include=optional` alone is NOT sufficient on Apple Silicon
# (linux/arm64) — npm/cli#4828 can still skip the rollup native package
# entry depending on how the optionalDependencies tree was resolved. To
# make the build portable across both linux/amd64 and linux/arm64 hosts,
# we additionally pull both rollup native binaries explicitly with
# --force so the appropriate one is present at `npm run build` time.
RUN rm -f package-lock.json \
    && npm install --include=optional --no-audit --no-fund --legacy-peer-deps \
    && npm install --no-save --no-audit --no-fund --legacy-peer-deps --force \
        @rollup/rollup-linux-arm64-gnu @rollup/rollup-linux-x64-gnu
COPY webui/ ./
RUN npm run build

# ── Stage 2: python runtime ─────────────────────────────────────────
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
# pyproject.toml force-includes webui/dist, so we need it present for
# both the deps-only and the --no-deps install steps to succeed.
COPY pyproject.toml README.md hatch_build.py ./
COPY --from=webui-builder /webui/dist ./webui/dist
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
