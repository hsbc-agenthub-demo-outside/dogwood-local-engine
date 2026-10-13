# Two binaries, one image: `dogwood-server` (the engine) and `dogwood-http` (the
# HTTP + OpenAPI adapter in front of it). The same image serves both because they
# are the same codebase and always ship together — and because the interesting part
# of this deployment is the uid and volume layout in `docker-compose.yml`, not the
# contents of the image.
#
# No `# syntax=` directive on purpose. Pinning a frontend version means pulling
# `docker/dockerfile` from the registry before the build can even start, which fails
# in any network where Docker Hub is not reachable (including most in China). The
# built-in frontend that ships with Docker Desktop supports everything used here,
# including `RUN --mount=type=cache`.
#
# Why Linux at all: the engine's control plane authorizes by kernel peer credential
# (`SO_PEERCRED`), which does not exist on macOS. On the host, that plane refuses
# every caller by design. In this container it works — which is the single biggest
# reason to run the engine in Docker on a Mac.

# ─── build ───────────────────────────────────────────────────────────
FROM rust:1-bookworm AS builder

WORKDIR /src
COPY . .

# The base `rust` image is `buildpack-deps`-based and already carries git and
# ca-certificates, which this build needs because the workspace pins
# `dogwood-language` at a git revision. Both are *asserted* rather than assumed: a
# base image that stops shipping one should fail here, with a message, instead of
# later as a confusing fetch error from cargo.
#
# Nothing is installed, so the build stage reaches no apt repository. The only
# network it needs is crates.io and the git remote — for a hermetic build, vendor
# the dependencies and replace the fetch with `cargo vendor`.
RUN git --version && cargo --version

# `--locked` so the image gets the dependency graph the tests ran against, not a
# freshly resolved one. The cache mounts keep the registry and the incremental build
# artifacts across rebuilds even though `COPY . .` invalidates the layer.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --locked -p dogwood-server -p dogwood-http \
 && mkdir -p /out \
 && cp target/release/dogwood-server target/release/dogwood-http /out/

# ─── runtime ─────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS runtime

# Two principals, and the split is the point:
#
#   10001 dogwood        owns the engine process, its store, and the control socket.
#   10002 dogwood-agent  runs the data-plane adapter — the face the monitored agent
#                        reaches. It is deliberately *not* in the engine's control
#                        allowlist and deliberately *not* the owner of the control
#                        socket, which is mode 0700. So the process that accepts
#                        events from the agent cannot author policy even if it is
#                        compromised: the OS refuses the connect() before a byte is
#                        exchanged.
#
# This is the privilege asymmetry `dogwood-server` documents for a bare-metal
# deployment — "the boundary is real only if the server runs as a different,
# lower-privileged uid than the monitored agent" — expressed in Dockerfile terms.
RUN groupadd --gid 10001 dogwood \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin dogwood \
 && groupadd --gid 10002 dogwood-agent \
 && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin dogwood-agent \
 && install -d -o dogwood -g dogwood -m 0700 /var/lib/dogwood \
 && install -d -o dogwood -g dogwood -m 0711 /var/lib/dogwood/run

COPY --from=builder /out/dogwood-server /usr/local/bin/dogwood-server
COPY --from=builder /out/dogwood-http /usr/local/bin/dogwood-http

# The healthcheck needs no `curl`: `dogwood-http healthcheck` speaks HTTP itself and
# `dogwood-server ping` speaks the data socket. That is the whole reason this image
# installs no packages — a smaller image, no apt repository in the build, and a
# healthcheck that works in an air-gapped registry.

# TWO directories exist for one reason: to seed the ownership of the two named
# volumes in `docker-compose.yml`. Docker initializes a *fresh* named volume with the
# ownership and mode of the image path it is mounted over, and a volume whose target
# does not exist in the image is created owned by root — which the engine's uid
# cannot chmod. Creating both paths here, with the right owner and mode, is what lets
# the engine start without the container ever running as root:
#
#   /var/lib/dogwood      0700  the store, the policy bundle, and control.sock
#   /var/lib/dogwood/run  0711  the data socket, deliberately traversable
#
# Mounted separately in compose (see the volume comment there), which is what makes
# the second one reachable by a different uid.
#
# Note what is NOT here: no `USER` directive. The services in `docker-compose.yml`
# run as different uids, so the image cannot pick one. Every documented way of
# running it states its uid explicitly.
VOLUME ["/var/lib/dogwood"]

CMD ["dogwood-http", "serve", "--help"]
