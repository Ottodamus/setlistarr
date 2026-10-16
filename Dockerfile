FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SETLISTARR_CONFIG_DIR=/config \
    SETLISTARR_PORT=8787

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /root/.cache

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config

VOLUME /config
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"SETLISTARR_PORT\",\"8787\")}/health', timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["setlistarr", "serve"]
