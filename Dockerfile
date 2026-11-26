FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    ASC_SCHEDULE=09:00 \
    ASC_DATA_DIR=/data \
    ASC_PRIVATE_KEY_PATH=/secrets/AuthKey.p8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="asc-bark" \
    org.opencontainers.image.description="Scheduled App Store Connect analytics summaries pushed to Bark" \
    org.opencontainers.image.source="https://github.com/feong/asc-bark" \
    org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin asc \
    && mkdir -p /data /secrets \
    && chown asc:asc /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY asc_bark.py .

USER asc

# serve waits for ASC_SCHEDULE, runs the fetch and push, then waits again.
# One-shot commands replace this: init, run, test-notification.
ENTRYPOINT ["python", "/app/asc_bark.py"]
CMD ["serve"]
