#!/usr/bin/with-contenv bash
# init-services completes the identity, timezone and custom settings first.
# The probe waits for private enrollment; s6 restarts it if it exits.
exec python3 -m caged.runtime.agents.environment browser
