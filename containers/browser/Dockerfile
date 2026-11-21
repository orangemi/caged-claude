FROM lscr.io/linuxserver/chromium:latest
ENV PYTHONPATH=/opt PYTHONSAFEPATH=1
COPY caged/__init__.py /opt/caged/
COPY caged/runtime/__init__.py /opt/caged/runtime/
COPY caged/shared /opt/caged/shared/
COPY caged/runtime/agents /opt/caged/runtime/agents/
COPY caged/runtime/checks/__init__.py caged/runtime/checks/environment.py /opt/caged/runtime/checks/
COPY containers/browser/rename-user.py /tmp/caged-rename-user.py
RUN python3 /tmp/caged-rename-user.py && rm /tmp/caged-rename-user.py
COPY containers/browser/network-policy.json /etc/chromium/policies/managed/caged-network.json
COPY --chmod=755 containers/browser/init-settings.sh /custom-cont-init.d/10-caged-settings
