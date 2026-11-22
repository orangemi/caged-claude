# Host-side launcher. Holds only dependencies: the project source is bind-
# mounted at its host path, so code changes never require rebuilding this
# image. run.sh tags it by this file's checksum and rebuilds when it changes.
FROM docker.io/library/alpine:3.22
RUN apk add --no-cache ca-certificates docker-cli docker-cli-buildx python3 py3-yaml tzdata
# CAGED_LAUNCHER makes run.sh execute Python directly instead of nesting
# another launcher, and tells the Python code it cannot see host loopback.
# DOCKER_CONFIG keeps the host's ~/.docker (credential helpers such as
# osxkeychain that do not exist here) out of the launcher.
ENV CAGED_LAUNCHER=1 PYTHONDONTWRITEBYTECODE=1 DOCKER_CONFIG=/tmp/docker-config
