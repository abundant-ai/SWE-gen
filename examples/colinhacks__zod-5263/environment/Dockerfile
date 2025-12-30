FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Enable corepack for pnpm support
RUN corepack enable

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone https://github.com/colinhacks/zod.git src && \
    cd src && \
    git fetch origin 1a3a535fa5ad44dddfd01f4ff80b3ac38b2f5c77 && \
    git checkout 1a3a535fa5ad44dddfd01f4ff80b3ac38b2f5c77 && \
    git submodule update --init --recursive

WORKDIR /app/src

# Set environment variables for testing
ENV CI=true
ENV NODE_ENV=test

# Install dependencies using pnpm
RUN pnpm install --frozen-lockfile

# Build the project (TypeScript compilation)
RUN pnpm run build

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

# Rebuild after applying bug.patch (TypeScript needs recompilation)
RUN pnpm run build

RUN rm -rf /app/src/.git

WORKDIR /app/src
