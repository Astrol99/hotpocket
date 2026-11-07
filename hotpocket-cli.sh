#!/bin/sh
# Installed as /usr/local/bin/hotpocket in the container (see setup.sh).
if [ "$(id -u)" = 0 ]; then
  exec setpriv --reuid=hotpocket --regid=hotpocket --init-groups /opt/hotpocket/.venv/bin/hotpocket "$@"
fi
exec /opt/hotpocket/.venv/bin/hotpocket "$@"
