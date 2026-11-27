# syntax=docker/dockerfile:1
# Multi-stage, multi-arch (linux/arm64 + linux/amd64) GridForge image:
#   Stage 1 (frontend):     Builds the MapLibre GL JS + Vite SPA (`web/dist`)
#   Stage 2 (runtime-base): Headless PyQGIS 3.40 LTR + GDAL + FastAPI/Uvicorn on Debian Trixie
#   Stage 3 (test):         CI test target adding pytest (not included in production runtime)
#   Stage 4 (runtime):      Lean production non-root image (default build target)

FROM node:22-slim AS frontend

WORKDIR /web
COPY web/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY web/index.html web/tsconfig.json web/vite.config.ts ./
COPY web/src/ ./src/
RUN npm run build

FROM debian:trixie-slim AS runtime-base

ENV DEBIAN_FRONTEND=noninteractive

# Install native Debian Trixie headless PyQGIS 3.40 LTR, GDAL, Osmium, Poppler, Xvfb, and OSS fonts.
# Intentionally excludes desktop `qgis` GUI package and amd64-only upstream qgis.org repos.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-qgis \
    python3-pip \
    python3-venv \
    gdal-bin \
    python3-gdal \
    osmium-tool \
    poppler-utils \
    xvfb \
    ca-certificates \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    curl \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Create virtualenv inheriting system site-packages (qgis, PyQt5, osgeo.gdal)
RUN python3 -m venv --system-site-packages /opt/venv \
    && echo 'export PATH="/opt/venv/bin:${PATH}"' > /etc/profile.d/gridforge-venv.sh
ENV PATH="/opt/venv/bin:${PATH}"

# Install Python runtime and web API dependencies before copying application code for layer caching
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir \
        "typer>=0.12" \
        "pydantic>=2.7" \
        "pyproj>=3.6" \
        "mgrs>=1.6.0" \
        "pygeomag==1.1.0" \
        "shapely>=2.0" \
        "httpx>=0.27" \
        "pypdf>=7.0.1" \
        "pillow>=13.0.0" \
        "fastapi>=0.115.12" \
        "starlette>=1.9" \
        "uvicorn[standard]>=0.30" \
        "h11>=0.16.0" \
        "python-multipart>=0.0.31"

# Create dedicated non-root runtime user (UID/GID 10001)
RUN groupadd --gid 10001 gridforge \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin gridforge

# Headless Qt, PROJ, GDAL /vsicurl/ COG streaming, bounded timeouts, and strict 16 GB-host memory defaults
ENV QT_QPA_PLATFORM=offscreen \
    XDG_RUNTIME_DIR=/tmp/runtime-gridforge \
    PROJ_DATA=/usr/share/proj \
    GDAL_CACHEMAX=192 \
    OMP_THREAD_LIMIT=1 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.vrt \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=67108864 \
    GDAL_HTTP_MAX_RETRY=3 \
    GDAL_HTTP_RETRY_DELAY=2 \
    GDAL_HTTP_TIMEOUT=30 \
    GDAL_HTTP_CONNECTTIMEOUT=10 \
    GRIDFORGE_WEB_DIST=/app/web/dist \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN mkdir -p /tmp/runtime-gridforge /tmp/gridforge-cache /data/jobs /data/packs /data/cache /data/samples \
    && chmod 700 /tmp/runtime-gridforge \
    && chown -R 10001:10001 /tmp/runtime-gridforge /tmp/gridforge-cache /data

WORKDIR /app

# Copy project metadata, config, styles/icons, source, and built SPA
COPY pyproject.toml README.md LICENSE ./
COPY config/ ./config/
COPY assets/ ./assets/
COPY src/ ./src/
COPY --from=frontend /web/dist /app/web/dist

RUN pip install --no-cache-dir --no-deps --no-build-isolation -e . \
    && chown -R 10001:10001 /app

FROM runtime-base AS test
RUN pip install --no-cache-dir "pytest>=8.0"
USER 10001:10001

FROM runtime-base AS runtime
USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/api/health" || exit 1

CMD ["python3", "-m", "gridforge.web.entrypoint"]
