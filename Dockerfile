# Aetherscope on Linux. USB passthrough works on Linux (unlike macOS Docker),
# so a container can reach a HackRF One on the host's USB bus.
#
#   docker build -t aetherscope .
#   docker run --rm --device=/dev/bus/usb -p 127.0.0.1:8765:8765 aetherscope
#
# Keep the published port bound to 127.0.0.1 and reach it over Tailscale or an
# SSH tunnel (same posture as the native install), or front it with the Caddy
# recipe in deploy/Caddyfile.example.
#
# NOTE: provided as a starting point; not yet verified on a real Linux + HackRF
# host. The readsb (ADS-B) build deps follow the wiedehopf/readsb docs.

FROM python:3.12-slim-bookworm

# Runtime SDR stack (sweep/capture/radio via hackrf; ISM decode via rtl-433 +
# SoapySDR) plus the build deps to compile readsb with HackRF support.
RUN apt-get update && apt-get install -y --no-install-recommends \
        hackrf libhackrf0 libhackrf-dev \
        soapysdr-tools soapysdr-module-hackrf \
        rtl-433 librtlsdr0 librtlsdr-dev \
        libusb-1.0-0 libusb-1.0-0-dev \
        zlib1g zlib1g-dev libzstd1 libzstd-dev libncurses6 libncurses-dev \
        build-essential git curl ca-certificates \
    && git clone --depth 1 https://github.com/wiedehopf/readsb /tmp/readsb \
    && make -C /tmp/readsb HACKRF=yes -j"$(nproc)" \
    && cp /tmp/readsb/readsb /usr/local/bin/readsb-hackrf \
    && rm -rf /tmp/readsb /var/lib/apt/lists/*

# uv (Astral), placed in a system path so the non-root user can run it
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && cp /root/.local/bin/uv /usr/local/bin/uv
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend ./backend
COPY frontend ./frontend
RUN uv sync

# Run as a non-root user. USB device access then requires the host's device
# node to be reachable by this user: add a udev rule and run with
# `--group-add plugdev`, or fall back to `--privileged` if your setup needs it.
RUN useradd --create-home --uid 1000 aether \
    && mkdir -p /captures && chown -R aether:aether /app /captures
USER aether

# Bind all interfaces inside the container; publish only to 127.0.0.1 on the host.
ENV AETHERSCOPE_HOST=0.0.0.0 \
    AETHERSCOPE_CAPTURES_DIR=/captures
VOLUME ["/captures"]
EXPOSE 8765
CMD ["uv", "run", "aetherscope"]
