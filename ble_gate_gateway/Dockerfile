ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

# Install Python, BlueZ, and build dependencies for Python wheels.
RUN apk add --no-cache \
    python3 \
    py3-pip \
    bluez \
    bluez-dev \
    bluez-deprecated \
    dbus \
    dbus-dev \
    musl-dev \
    gcc \
    linux-headers

WORKDIR /app

# Copy Python gateway source
COPY app/requirements.txt .
RUN python3 -m venv /opt/ble-gateway-venv \
    && /opt/ble-gateway-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/ble-gateway-venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app/gateway.py app/ble_client.py app/ble_tools.py app/mqtt_client.py app/ha_discovery.py ./
COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PATH="/opt/ble-gateway-venv/bin:${PATH}"

CMD ["/run.sh"]
