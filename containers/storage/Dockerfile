# Short-lived helper for the image state backend (docs/stateful-service.md):
# creates ext4 images and attaches or detaches their loop devices in the
# Docker VM. It runs privileged only for losetup, without network, and never
# alongside the cage's own containers.
FROM docker.io/library/debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update \
    && apt-get install -y --no-install-recommends e2fsprogs \
    && rm -rf /var/lib/apt/lists/*
