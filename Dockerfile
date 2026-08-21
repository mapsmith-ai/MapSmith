FROM python:3.12-slim

# MCP Registry ownership proof: must equal the server.json "name" exactly.
LABEL io.modelcontextprotocol.server.name="io.github.mapsmith-ai/mapsmith"

# Geospatial wheels (pyogrio/shapely/pyproj) bundle their native libs; no system GDAL needed.
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# raster + whitebox extras ship manylinux x86_64 wheels; the image is amd64-only.
RUN pip install --no-cache-dir ".[raster,whitebox]"

# Unprivileged by default (#19). uid 1000 on purpose rather than a high system
# uid: the supported way to run this is a bind mount of your own data directory,
# which on Linux is usually owned by the first human user — 1000. If yours is
# not, pass `--user $(id -u):$(id -g)`.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin mapsmith \
    && mkdir -p /data \
    && chown mapsmith:mapsmith /data

# Workspace for datasets: mount your data here. Confined BY DEFAULT — the
# supported path used to start unconfined unless the operator remembered `-e`,
# which is the wrong way round for a default.
VOLUME ["/data"]
WORKDIR /data
ENV MAPSMITH_WORKSPACE=/data \
    HOME=/home/mapsmith \
    MPLCONFIGDIR=/home/mapsmith/.config/matplotlib

USER mapsmith

ENTRYPOINT ["mapsmith"]
