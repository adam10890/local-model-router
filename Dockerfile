FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY local_model_router ./local_model_router
RUN pip install --no-cache-dir ".[mcp,agents]" \
    && useradd --create-home --uid 10001 imperium \
    && mkdir -p /app/data \
    && chown -R imperium:imperium /app

# Fleet config is mounted at runtime (see docker-compose.yml).
ENV OBSERVER_HOST=0.0.0.0 \
    OBSERVER_PORT=9000 \
    A0_LMM_ROUTER_CONFIG=/app/conf/llama_cpp_servers.yaml \
    A0_FLEET_STATE_DB=/app/data/fleet_state.sqlite3 \
    A0_LMM_ROUTER_STATS_PATH=/app/data/failover_stats.json

USER imperium

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=3)"]

# Public bind inside the container is safe — compose maps it to localhost.
# An API key is still required unless explicitly waived (see service docs).
CMD ["python", "-m", "local_model_router", "serve"]
