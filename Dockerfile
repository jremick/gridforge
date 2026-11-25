# syntax=docker/dockerfile:1
# Multi-arch (linux/arm64 + linux/amd64) headless PyQGIS 3.40 LTR image on Debian Trixie
FROM debian:trixie-slim

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

# Install Python runtime and test dependencies before copying application code for layer caching
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
        "pytest>=8.0"

# Headless Qt, PROJ, GDAL /vsicurl/ COG streaming, and memory-bounded runtime defaults
ENV QT_QPA_PLATFORM=offscreen \
    XDG_RUNTIME_DIR=/tmp/runtime-root \
    PROJ_DATA=/usr/share/proj \
    GDAL_CACHEMAX=256 \
    OMP_THREAD_LIMIT=1 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.vrt \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=67108864 \
    GDAL_HTTP_MAX_RETRY=3 \
    GDAL_HTTP_RETRY_DELAY=2 \
    GRIDFORGE_CACHE_DIR=/tmp/gridforge-cache \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN mkdir -p /tmp/runtime-root /tmp/gridforge-cache \
    && chmod 700 /tmp/runtime-root

WORKDIR /app

# Copy project metadata, config, styles, and source, then install editable (volume-mount friendly)
COPY pyproject.toml README.md LICENSE ./
COPY config/ ./config/
COPY assets/ ./assets/
COPY src/ ./src/

RUN pip install --no-cache-dir --no-deps --no-build-isolation -e .

CMD ["gridforge", "--help"]
