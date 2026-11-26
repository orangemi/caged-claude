FROM docker.io/metacubex/mihomo:latest AS core
FROM docker.io/library/alpine:3.22
RUN apk add --no-cache ca-certificates iptables ipset iproute2 python3 curl tcpdump py3-websocket-client tzdata
COPY --from=core /mihomo /usr/local/bin/mihomo
COPY caged/__init__.py /opt/caged/
COPY caged/runtime/__init__.py /opt/caged/runtime/
COPY caged/shared /opt/caged/shared/
COPY caged/runtime/network /opt/caged/runtime/network/
COPY caged/runtime/checks/__init__.py caged/runtime/checks/network.py caged/runtime/checks/egress.py caged/runtime/checks/policy.py caged/runtime/checks/browser.py /opt/caged/runtime/checks/
ENV PYTHONPATH=/opt PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-u", "-m", "caged.runtime.network.main"]
