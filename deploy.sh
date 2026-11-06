#!/bin/sh
# Create or update the claude-gateway incus container.
# Run it on the incus host itself, or from another machine with HOST=<ssh host>.
# Safe to re-run: the first run creates the container; later runs push the
# current code and restart the service. Account tokens and developer keys in
# the container are never touched.
set -eu

HOST="${HOST:-local}"
NAME="${NAME:-claude-gateway}"
DIR=$(cd "$(dirname "$0")" && pwd)

if [ "$HOST" = local ]; then
  remote() { sh -c "$*"; }   # same word-joining as ssh, so both paths behave alike
  PREFIX=""
else
  remote() { ssh -o BatchMode=yes "$HOST" "$@"; }
  PREFIX="ssh $HOST "
fi
in_ct() { remote incus exec "$NAME" -- "$@"; }          # simple commands only (no spaces in args)
in_ct_sh() { remote "incus exec $NAME -- sh -c '$1'"; }  # a shell snippet (no single quotes in it)

if ! remote incus info "$NAME" >/dev/null 2>&1; then
  echo "==> Creating container $NAME on $HOST"
  # Create it stopped: the tun device (for Tailscale) can't be hot-added.
  remote incus init images:ubuntu/24.04 "$NAME" \
    -c limits.cpu=2 -c limits.memory=1GiB -c boot.autostart=true
  remote incus config device add "$NAME" tun unix-char \
    source=/dev/net/tun path=/dev/net/tun required=false >/dev/null
  remote incus start "$NAME"

  echo "==> Waiting for the container's network"
  i=0
  until in_ct getent hosts pkgs.tailscale.com >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -lt 60 ] || { echo "No network in $NAME" >&2; exit 1; }
    sleep 2
  done

  echo "==> Installing python3 and Tailscale"
  in_ct_sh "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq &&
    apt-get install -y -qq python3 curl ca-certificates >/dev/null &&
    curl -fsSL https://tailscale.com/install.sh | sh >/dev/null"
  in_ct useradd --system --home-dir /var/lib/claude-gateway --shell /usr/sbin/nologin claude-gateway
fi

echo "==> Installing the gateway"
if [ "$HOST" = local ]; then
  src="$DIR"
else
  src=$(remote mktemp -d)
  scp -q "$DIR/claude-gateway" "$DIR/claude-gateway.service" "$DIR/hardening.conf" "$HOST:$src/"
fi
remote "incus file push -q $src/claude-gateway $NAME/usr/local/bin/claude-gateway --mode 0755 &&
        incus file push -q $src/claude-gateway.service $NAME/etc/systemd/system/claude-gateway.service --mode 0644 &&
        incus file push -q -p $src/hardening.conf $NAME/etc/systemd/system/claude-gateway.service.d/zzzz-hardening.conf --mode 0644"
[ "$HOST" = local ] || remote rm -rf "$src"
in_ct_sh "systemctl daemon-reload && systemctl enable -q claude-gateway && systemctl restart claude-gateway"

sleep 2
if ! in_ct systemctl is-active -q claude-gateway; then
  echo "The service failed to start:" >&2
  in_ct journalctl -u claude-gateway -n 20 --no-pager >&2
  exit 1
fi
echo "==> Gateway running: $(in_ct curl -fsS http://127.0.0.1:8080/_gateway/health)"

if in_ct tailscale status >/dev/null 2>&1; then
  echo "==> Tailscale connected: https://$(in_ct tailscale status --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
else
  cat <<EOF

Next: join the container to Tailscale (one time).
  ${PREFIX}incus exec $NAME -- tailscale up --hostname=$NAME
Open the link it prints and approve the machine, then serve the gateway over HTTPS:
  ${PREFIX}incus exec $NAME -- tailscale serve --bg 8080
EOF
fi
