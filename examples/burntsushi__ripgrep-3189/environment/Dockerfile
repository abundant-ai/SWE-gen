FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain (version 1.85 as per Cargo.toml rust-version)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.85.0
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone https://github.com/BurntSushi/ripgrep.git src && \
    cd src && \
    git fetch origin 12b81e5ba3468269d60521902db3e6fd5b3d3826 && \
    git checkout 12b81e5ba3468269d60521902db3e6fd5b3d3826 && \
    git submodule update --init --recursive

WORKDIR /app/src

# Set environment variables for CI and testing
ENV RUST_BACKTRACE=1

# Fetch dependencies
RUN cargo fetch

# Build ripgrep and all crates
RUN cargo build --verbose --workspace

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

# Rebuild after applying bug.patch (required for Rust)
RUN cargo build --verbose --workspace

RUN rm -rf /app/src/.git

WORKDIR /app/src
