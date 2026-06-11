FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY local_model_router ./local_model_router
RUN pip install --no-cache-dir -e ".[mcp]"

# Fleet config is mounted at runtime (see docker-compose.yml).
ENV OBSERVER_HOST=0.0.0.0 \
    OBSERVER_PORT=9000 \
    A0_LMM_ROUTER_CONFIG=/app/conf/llama_cpp_servers.yaml

EXPOSE 9000

# Public bind inside the container is safe — compose maps it to localhost.
# An API key is still required unless explicitly waived (see service docs).
CMD ["python", "-m", "local_model_router", "serve"]
