ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

# Install BlueZ and dbus dependencies
RUN apk add --no-cache \
    bluez \
    bluez-dev \
    dbus \
    dbus-dev \
    musl-dev \
    gcc \
    linux-headers

WORKDIR /app

# Copy Python gateway source
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/gateway.py app/ble_client.py app/ble_tools.py app/mqtt_client.py app/ha_discovery.py ./
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
