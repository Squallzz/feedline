# Feedline in a container. One way to run it; install.sh (systemd) stays the other.
#   docker run -d -p 8081:8081 -e FEEDLINE_PRINTER=192.168.1.50 -v feedline-data:/data ghcr.io/squallzz/feedline
# Settings, custom graphics, backups and an optional config.json live in /data.
FROM python:3.13-alpine

LABEL org.opencontainers.image.title="Feedline" \
      org.opencontainers.image.description="Klipper / Moonraker toolchanger and filament dashboard" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY feedline.html /app/index.html
COPY feedline_server.py /app/feedline_server.py
COPY graphics/ /app/graphics/
COPY tools/spoolman_autoadd.py /app/tools/spoolman_autoadd.py
COPY config.example.json /app/config.example.json
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
 && chmod 755 /usr/local/bin/docker-entrypoint.sh \
 && adduser -D -H -u 1000 feedline \
 && mkdir -p /data \
 && chown feedline:feedline /data

USER feedline
VOLUME ["/data"]
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('FEEDLINE_PORT', '8081'), timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["server"]
