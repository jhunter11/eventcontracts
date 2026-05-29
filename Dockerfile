# syntax=docker/dockerfile:1.7
#
# Build stage: pinned Rust toolchain. Pin a digest in CI (`docker pull` →
# inspect → replace `:latest`-equivalent here) for fully reproducible builds.
FROM rust:1.83-bookworm@sha256:a45bf1f5d9af0a23b26703b3500d70af1abff7f984a7abef5a104b42c02a292b AS builder

WORKDIR /app
COPY rust ./rust
WORKDIR /app/rust
# Use `--locked` so a corrupted/missing Cargo.lock fails the build instead
# of silently regenerating one.
RUN cargo build --release --locked -p eventcontracts-live-runner

# Runtime stage: slim Debian. `ca-certificates` for TLS to Kalshi REST/WS.
# `tini` to reap zombie processes and forward signals cleanly to the
# entrypoint binary (Ctrl-C / SIGTERM trigger the runner's shutdown hooks).
FROM debian:bookworm-slim@sha256:0104b334637a5f19aa9c983a91b54c89887c0984081f2068983107a6f6c21eeb

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 ec \
    && useradd --system --uid 1000 --gid ec --home /app --shell /usr/sbin/nologin ec \
    && mkdir -p /app /app/configs /app/artifacts \
    && chown -R ec:ec /app

COPY --from=builder /app/rust/target/release/eventcontracts-live-runner /usr/local/bin/eventcontracts-live-runner

# Non-root execution. The runner only reads configs/artifacts (mount
# read-only at runtime) and writes to stdout/stderr + the optional
# --metrics-json path (mount that as a writable volume if used).
USER ec
WORKDIR /app

# Healthcheck: `eventcontracts-live-runner --help` exits 0 if the binary is
# intact. A liveness check against the metrics-json mtime is more accurate
# for production but requires a sidecar; this gates "is the image broken".
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/usr/local/bin/eventcontracts-live-runner", "--help"]

# `tini -g` forwards signals to the whole process group so Ctrl-C reaches
# the runner's shutdown handler (which cancels open orders).
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/eventcontracts-live-runner"]
