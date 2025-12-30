FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js runtime (CI tests on multiple versions, using Node 20 as default)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone https://github.com/axios/axios.git src && \
    cd src && \
    git fetch origin ba7bfe1203e8bec2a3d261bcffb5ffbaf913b74f && \
    git checkout ba7bfe1203e8bec2a3d261bcffb5ffbaf913b74f && \
    git submodule update --init --recursive

WORKDIR /app/src

# Install dependencies using npm ci (uses package-lock.json)
RUN npm ci

# Build the project (creates distribution bundles)
RUN npm run build

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

# Rebuild after patching to ensure dist/ bundles reflect the buggy state
RUN npm run build

RUN rm -rf /app/src/.git

WORKDIR /app/src
