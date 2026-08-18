FROM python:3.12-slim

# Geospatial wheels (pyogrio/shapely/pyproj) bundle their native libs; no system GDAL needed.
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# raster + whitebox extras ship manylinux x86_64 wheels; the image is amd64-only.
RUN pip install --no-cache-dir ".[raster,whitebox]"

# Workspace for datasets: mount your data here
VOLUME ["/data"]
WORKDIR /data

ENTRYPOINT ["mapsmith"]
