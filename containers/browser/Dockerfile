FROM lscr.io/linuxserver/chromium:latest

# This image is a kiosk browser, not a desktop. The hardening switches remove
# the window controls, right-click menu, terminal launchers, sudo and external
# open helpers; the Selkies sidebar and sharing/file-transfer UI are disabled.
# RESTART_APP is explicitly false so HARDEN_OPENBOX does not silently respawn
# Chromium; session.sh shows the crash screen instead.
ENV PIXELFLUX_WAYLAND=false
ENV HARDEN_DESKTOP=true
ENV HARDEN_OPENBOX=true
ENV RESTART_APP=false
ENV NO_DECOR=true
ENV DISABLE_WINDOW_BUTTONS=true
ENV SELKIES_UI_SHOW_SIDEBAR="false|locked"
ENV SELKIES_UI_SIDEBAR_SHOW_FILES=false
ENV SELKIES_UI_SIDEBAR_SHOW_APPS=false
ENV SELKIES_ENABLE_SHARING=false
ENV SELKIES_ENABLE_SHARED=false
ENV SELKIES_ENABLE_PLAYER2=false
ENV SELKIES_ENABLE_PLAYER3=false
ENV SELKIES_ENABLE_PLAYER4=false
ENV SELKIES_ENABLE_CLIPBOARD="false|locked"
ENV SELKIES_SECOND_SCREEN=false
ENV SELKIES_PRINTING_ENABLED=false
ENV SELKIES_FILE_TRANSFERS=none
ENV SELKIES_COMMAND_ENABLED=false
ENV PYTHONPATH=/opt PYTHONSAFEPATH=1
COPY caged/__init__.py /opt/caged/
COPY caged/runtime/__init__.py /opt/caged/runtime/
COPY caged/shared /opt/caged/shared/
COPY caged/runtime/agents /opt/caged/runtime/agents/
COPY caged/runtime/checks/__init__.py caged/runtime/checks/environment.py /opt/caged/runtime/checks/
COPY containers/browser/rename-user.py /tmp/caged-rename-user.py
RUN python3 /tmp/caged-rename-user.py && rm /tmp/caged-rename-user.py
COPY containers/browser/network-policy.json /etc/chromium/policies/managed/caged-network.json
COPY --chmod=755 containers/browser/init-profile-lock.sh /custom-cont-init.d/05-caged-profile-lock
COPY --chmod=755 containers/browser/init-settings.sh /custom-cont-init.d/10-caged-settings
COPY --chmod=755 containers/browser/session.sh /usr/local/bin/caged-browser-session
COPY --chmod=644 containers/browser/crash.html /usr/share/caged/crash.html
