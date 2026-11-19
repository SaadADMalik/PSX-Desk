# One image recipe for every Python service in the workspace.
#   docker build --build-arg PACKAGE=llm-gateway .
# Only the named package and its dependencies are installed.
FROM python:3.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first so code edits do not reinstall them.
COPY pyproject.toml uv.lock ./
COPY libs/contracts/pyproject.toml libs/contracts/
COPY libs/agentkit/pyproject.toml libs/agentkit/
COPY libs/marketdata/pyproject.toml libs/marketdata/
COPY libs/eventbus/pyproject.toml libs/eventbus/
COPY services/llm_gateway/pyproject.toml services/llm_gateway/
COPY services/orchestrator/pyproject.toml services/orchestrator/
COPY services/scheduler/pyproject.toml services/scheduler/
COPY agents/baseline/pyproject.toml agents/baseline/
COPY agents/technical/pyproject.toml agents/technical/
COPY agents/fundamentals/pyproject.toml agents/fundamentals/
COPY agents/news/pyproject.toml agents/news/
COPY agents/debate/pyproject.toml agents/debate/
COPY mcp_servers/market_data/pyproject.toml mcp_servers/market_data/
COPY mcp_servers/memory/pyproject.toml mcp_servers/memory/
COPY mcp_servers/broker/pyproject.toml mcp_servers/broker/
COPY agents/portfolio/pyproject.toml agents/portfolio/
COPY agents/advisor/pyproject.toml agents/advisor/
COPY services/dashboard/pyproject.toml services/dashboard/
COPY mcp_servers/official_web/pyproject.toml mcp_servers/official_web/
ARG PACKAGE
RUN uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}"

COPY libs libs
COPY services services
COPY agents agents
COPY mcp_servers mcp_servers
RUN uv sync --frozen --no-dev --package "${PACKAGE}"

# The memory server embeds text locally. Download the model at build time, into the
# image, so a running container never fetches code or weights from the internet.
ENV FASTEMBED_CACHE_PATH=/app/models
RUN if [ "${PACKAGE}" = "memory-mcp" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"; \
    fi

# Never run services as root. A home dir (psxdata caches there) and one writable
# data dir; everything else stays read-only to the service.
RUN useradd --system --create-home --uid 10001 app \
    && mkdir -p /app/data /app/models && chown app /app/data && chown -R app /app/models
USER app
