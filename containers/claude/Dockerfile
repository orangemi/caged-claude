FROM node:24-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git gosu locales-all python3 ripgrep tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@latest \
    && npm cache clean --force \
    && usermod -l abc -d /config -m node \
    && groupmod -n abc node
RUN usermod -l claude-user -c claude-user abc \
    && groupmod -n claude-user abc
COPY --chmod=755 containers/claude/entrypoint.sh /usr/local/bin/caged-entrypoint
COPY caged/__init__.py /opt/caged/
COPY caged/runtime/__init__.py /opt/caged/runtime/
COPY caged/shared /opt/caged/shared/
COPY caged/runtime/agents /opt/caged/runtime/agents/
COPY caged/runtime/checks/__init__.py caged/runtime/checks/claude.py caged/runtime/checks/network.py /opt/caged/runtime/checks/
ENV HOME=/config CLAUDE_CONFIG_DIR=/config/.claude DISABLE_AUTOUPDATER=1 PYTHONPATH=/opt PYTHONSAFEPATH=1
WORKDIR /config
ENTRYPOINT ["/usr/local/bin/caged-entrypoint"]
CMD ["sleep", "infinity"]
