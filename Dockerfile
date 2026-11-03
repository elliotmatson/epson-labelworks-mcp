# Debian's python3 is built with BlueZ headers, so socket.AF_BLUETOOTH works.
# The official python:*-slim images are not, which breaks the rfcomm transport.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv libusb-1.0-0 fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir . \
 && /opt/venv/bin/python -c "import socket; assert socket.AF_BLUETOOTH"

ENV PATH=/opt/venv/bin:$PATH \
    LW_HOST=0.0.0.0 \
    LW_PORT=8765 \
    LW_DRY_RUN_DIR=/data/jobs \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"LW_PORT\"]}/healthz')" || exit 1

ENTRYPOINT ["labelworks"]
CMD ["serve"]
