# Harbor Task Checker - GitHub Action Container
#
# This container includes:
# - Python 3.13 with uv
# - taskgen CLI with all dependencies
# - Docker CLI (for Harbor validation)
# - Claude Code CLI (for task generation)

FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/abundant-ai/taskgen"
LABEL org.opencontainers.image.description="Harbor Task Checker GitHub Action"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    jq \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Install Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /taskgen

# Copy taskgen source and install
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install taskgen with all dependencies
RUN uv sync --frozen

# Copy action entrypoint
COPY action/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set up environment
ENV PYTHONUNBUFFERED=1
ENV TASKGEN_ACTION=1

ENTRYPOINT ["/entrypoint.sh"]

