# The image only seeds a new instance and provides the container shell. After
# the first start, /etc, /usr, /var, /opt, /srv, /root and /home/claude come
# from state/<instance>/ on the host (docs/stateful-service.md), so changing
# this file affects new instances and `reset`, not existing environments.
FROM docker.io/library/node:24-trixie-slim@sha256:af02d57b57719b9657a45199a79264a9437940cd55b3c7b3fc99615654f124a0 AS node

FROM docker.io/library/debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ARG CLAUDE_CODE_VERSION=2.1.330
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git gosu less locales-all openssh-client \
        procps python3 ripgrep rsync sudo tar tzdata xz-utils \
    && rm -rf /var/lib/apt/lists/*
# Node.js from the official image built on the same Debian release.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node /usr/local/include/node /usr/local/include/node
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && groupadd --gid 1000 claude-user \
    && useradd --uid 1000 --gid 1000 --home-dir /home/claude --create-home --shell /bin/bash claude-user \
    && install -d -m 750 /etc/sudoers.d
# The entrypoint and the probes are bind-mounted read-only from the project
# at /caged-runtime, outside the persisted directories.
ENV HOME=/home/claude CLAUDE_CONFIG_DIR=/home/claude/.claude DISABLE_AUTOUPDATER=1
WORKDIR /home/claude
# Not `docker run --init`: Docker mounts its init at /sbin/docker-init, which
# the persisted /usr mount hides (merged-usr). The runtime ships its own.
ENTRYPOINT ["/usr/bin/python3", "/caged-runtime/caged/runtime/init.py", "--agent", "claude", "/bin/bash", "/caged-runtime/entrypoint.sh"]
CMD ["sleep", "infinity"]
