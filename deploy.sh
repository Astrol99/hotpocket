#!/bin/sh
# Create or update the hotpocket incus container.
# Run it on the incus host itself, or from another machine with HOST=<ssh host>.
# Safe to re-run: every step skips work that is already done. Account tokens
# and developer keys in the container are never touched.
set -eu

HOST="${HOST:-local}"
NAME="${NAME:-hotpocket}"
DIR=$(cd "$(dirname "$0")" && pwd)

if [ "$HOST" = local ]; then
  remote() { "$@"; }
  PREFIX=""
else
  remote() { ssh -o BatchMode=yes "$HOST" "$@"; }  # arguments must not contain spaces
  PREFIX="ssh $HOST "
fi
in_ct() { remote incus exec "$NAME" -- "$@"; }

if ! remote incus info "$NAME" >/dev/null 2>&1; then
  echo "==> Creating container $NAME on $HOST"
  # Created stopped: the tun device (for Tailscale) can't be hot-added.
  remote incus init images:ubuntu/24.04 "$NAME" -c limits.cpu=2 -c limits.memory=1GiB -c boot.autostart=true
  remote incus config device add "$NAME" tun unix-char source=/dev/net/tun path=/dev/net/tun required=false >/dev/null
  remote incus start "$NAME"
fi

i=0
until in_ct getent hosts pkgs.tailscale.com >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "No network in $NAME" >&2; exit 1; }
  sleep 2
done

in_ct rm -rf /opt/hotpocket/src
in_ct mkdir -p /opt/hotpocket
tar -C "$DIR" --exclude=__pycache__ -cf - pyproject.toml uv.lock src setup.sh hotpocket.service hardening.conf |
  in_ct tar -C /opt/hotpocket --no-same-owner -xf -
in_ct sh /opt/hotpocket/setup.sh

if in_ct tailscale status >/dev/null 2>&1; then
  echo "==> Tailscale: $(in_ct tailscale serve status | head -1)"
else
  cat <<EOF

Next: join the container to Tailscale (one time).
  ${PREFIX}incus exec $NAME -- tailscale up --hostname=$NAME
Open the link it prints and approve the machine, then serve the gateway over HTTPS:
  ${PREFIX}incus exec $NAME -- tailscale serve --bg 8080
EOF
fi
