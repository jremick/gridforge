# syntax=docker/dockerfile:1
# Multi-stage, multi-arch (linux/arm64 + linux/amd64) GridForge image:
#   Stage 1 (frontend): Builds the MapLibre GL JS + Vite SPA (`web/dist`) and single-file MCP App (`mcp_app.html`)
#   Stage 2 (runtime):  Headless PyQGIS 3.40 LTR + GDAL + FastAPI/Uvicorn on Debian Trixie

FROM node:22-slim AS frontend

WORKDIR /web
COPY web/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY web/index.html web/tsconfig.json web/vite.config.ts web/vite.mcp-app.config.ts ./
COPY web/src/ ./src/
COPY web/mcp-app/ ./mcp-app/
RUN mkdir -p /src/gridforge/mcp/assets \
    && npm run build \
    && npm run build:mcp-app

FROM debian:trixie-slim AS runtime

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

# Install Python runtime, web API, and test dependencies before copying application code for layer caching
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir \
        "typer>=0.12" \
        "pydantic>=2.7" \
        "pyproj>=3.6" \
        "mgrs>=1.6.0" \
        "pygeomag==1.1.0" \
        "shapely>=2.0" \
        "httpx>=0.27" \
        "pypdf>=4.2" \
        "pillow>=10.3" \
        "fastapi>=0.115" \
        "starlette>=1.9" \
        "uvicorn[standard]>=0.30" \
        "python-multipart>=0.0.9" \
        "pytest>=8.0"

# Headless Qt, PROJ, GDAL /vsicurl/ COG streaming, and strict 16 GB-host memory-bounded defaults
ENV QT_QPA_PLATFORM=offscreen \
    XDG_RUNTIME_DIR=/tmp/runtime-root \
    PROJ_DATA=/usr/share/proj \
    GDAL_CACHEMAX=192 \
    OMP_THREAD_LIMIT=1 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.vrt \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=67108864 \
    GDAL_HTTP_MAX_RETRY=3 \
    GDAL_HTTP_RETRY_DELAY=2 \
    GRIDFORGE_WEB_DIST=/app/web/dist \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN mkdir -p /tmp/runtime-root /tmp/gridforge-cache /data/jobs /data/packs /data/cache /data/samples \
    && chmod 700 /tmp/runtime-root

WORKDIR /app

# Copy project metadata, config, styles/icons, source, built SPA, and single-file MCP App bundle
COPY pyproject.toml README.md LICENSE ./
COPY config/ ./config/
COPY assets/ ./assets/
COPY src/ ./src/
COPY --from=frontend /web/dist /app/web/dist
COPY --from=frontend /src/gridforge/mcp/assets/mcp_app.html /app/src/gridforge/mcp/assets/mcp_app.html

RUN pip install --no-cache-dir --no-deps --no-build-isolation -e .

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/api/health" || exit 1

CMD ["python3", "-m", "gridforge.web.entrypoint"]
