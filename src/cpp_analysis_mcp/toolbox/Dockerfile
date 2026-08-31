# The toolbox: every tool the container engine runs, pinned to one Ubuntu's versions.
# Pulled once and cached; a rebuilt image gets a new digest, and the digest sits in the
# capability-cache fingerprint, so every cached answer retires with it (ADR-0004).
FROM ubuntu:24.04

LABEL org.opencontainers.image.source="https://github.com/GusDawn123/cpp-analysis-mcp"
LABEL org.opencontainers.image.description="C++ analysis toolbox: clang, clang-tidy, sanitizer runtimes, cmake, ninja"

# git and ca-certificates stay: CMake FetchContent needs them, and a project build that
# cannot fetch its pinned dependency would fail for a reason nobody could see
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        clang-18 \
        clang-tidy-18 \
        libclang-rt-18-dev \
        llvm-18 \
        cmake \
        ninja-build \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/clang-18 /usr/local/bin/clang \
    && ln -s /usr/bin/clang++-18 /usr/local/bin/clang++ \
    && ln -s /usr/bin/clang-tidy-18 /usr/local/bin/clang-tidy \
    # without the symbolizer, sanitizers still catch bugs but report frames with no
    # file or line -- the WSL setup hit the same trap, getting-started documents it
    && ln -s /usr/lib/llvm-18/bin/llvm-symbolizer /usr/local/bin/llvm-symbolizer
