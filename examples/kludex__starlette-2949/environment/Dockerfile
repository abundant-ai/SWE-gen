FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python runtime
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone https://github.com/Kludex/starlette.git src && \
    cd src && \
    git fetch origin 0d6b01624d6a5072dc994fd8f461cc4feb415dbf && \
    git checkout 0d6b01624d6a5072dc994fd8f461cc4feb415dbf && \
    git submodule update --init --recursive

WORKDIR /app/src

# Set environment variables for testing
ENV GITHUB_ACTIONS=true

# Install dependencies using uv
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -r requirements.txt

# Add venv to PATH
ENV PATH="/opt/venv/bin:${PATH}"

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
