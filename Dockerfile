# PodWave — app + background job server
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PODWAVE_DATA=/data \
    PODWAVE_STATIC=/app/public \
    PODWAVE_BIND=0.0.0.0 \
    PODWAVE_PORT=8080

WORKDIR /app
COPY server/ /app/server/
COPY public/ /app/public/

# uid 1000 matches a normal host user, so a ./data bind mount stays writable
RUN mkdir -p /data && chown python /data
USER python
VOLUME /data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1:8080/api/health >/dev/null || exit 1
CMD ["python3", "/app/server/podwave.py"]
