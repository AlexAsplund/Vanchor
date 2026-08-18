# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock \
    && pip install --no-cache-dir --prefix=/install smbus2==0.5.0
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install --no-deps .

# picotool: flashes the helm-Pico firmware from the app (Devices → Motor →
# Helm firmware). Built from source — Debian bookworm has no package. The
# pico-sdk clone is headers-only input for the build; nothing of it ships.
FROM python:3.12-slim-bookworm AS picotool
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake g++ make pkg-config libusb-1.0-0-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 2.1.1 https://github.com/raspberrypi/pico-sdk /pico-sdk \
    && git clone --depth 1 --branch 2.1.1 https://github.com/raspberrypi/picotool /picotool-src \
    && cmake -S /picotool-src -B /picotool-build \
         -DPICO_SDK_PATH=/pico-sdk -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /picotool-build -j \
    && cmake --install /picotool-build --prefix /opt/picotool

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/AlexAsplund/Vanchor" \
      org.opencontainers.image.title="vanchor" \
      org.opencontainers.image.description="GPS anchoring / autopilot for trolling motors"
# network-manager provides nmcli for the WiFi setup card (/api/system/wifi).
# The NM daemon is NOT started in the container — nmcli only talks D-Bus
# to the host NM daemon via the socket bind-mount in docker-compose.yml.
# BENCH-VERIFY: polkit policy for uid-0-in-container D-Bus NM access.
# libusb-1.0-0 is picotool's only runtime dependency.
RUN apt-get update && apt-get install -y --no-install-recommends \
        network-manager libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
COPY --from=picotool /opt/picotool/bin/picotool /usr/local/bin/picotool
ENV VANCHOR_HOST=0.0.0.0 \
    VANCHOR_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/state', timeout=4).status == 200 else 1)"]
CMD ["vanchor"]
