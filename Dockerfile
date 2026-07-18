# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock \
    && pip install --no-cache-dir --prefix=/install smbus2==0.5.0
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install --no-deps .

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/AlexAsplund/Vanchor" \
      org.opencontainers.image.title="vanchor" \
      org.opencontainers.image.description="GPS anchoring / autopilot for trolling motors"
# network-manager provides nmcli for the WiFi setup card (/api/system/wifi).
# The NM daemon is NOT started in the container — nmcli only talks D-Bus
# to the host NM daemon via the socket bind-mount in docker-compose.yml.
# BENCH-VERIFY: polkit policy for uid-0-in-container D-Bus NM access.
RUN apt-get update && apt-get install -y --no-install-recommends \
        network-manager \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
ENV VANCHOR_HOST=0.0.0.0 \
    VANCHOR_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/state', timeout=4).status == 200 else 1)"]
CMD ["vanchor"]
