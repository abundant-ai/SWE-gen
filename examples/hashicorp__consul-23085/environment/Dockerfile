FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Go 1.25.4
RUN curl -fsSL https://go.dev/dl/go1.25.4.linux-amd64.tar.gz | tar -C /usr/local -xzf -
ENV PATH="/usr/local/go/bin:${PATH}"

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone https://github.com/hashicorp/consul.git src && \
    cd src && \
    git fetch origin 5ef3106d1eb1e8eac2c81cb9b6a99e16c7810cc9 && \
    git checkout 5ef3106d1eb1e8eac2c81cb9b6a99e16c7810cc9 && \
    git submodule update --init --recursive

WORKDIR /app/src

RUN go mod download

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
