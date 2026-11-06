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

  echo "==> Installing Python and Tailscale"
  in_ct_sh "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq &&
    apt-get install -y -qq python3 curl ca-certificates >/dev/null &&
    curl -fsSL https://tailscale.com/install.sh | sh >/dev/null"
  in_ct useradd --system --home-dir /var/lib/claude-gateway --shell /usr/sbin/nologin claude-gateway
fi

echo "==> Installing uv"
in_ct_sh "command -v uv >/dev/null ||
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null"

echo "==> Installing the gateway"
in_ct_sh "rm -rf /opt/claude-gateway/src && mkdir -p /opt/claude-gateway"
tar -C "$DIR" --exclude=__pycache__ -cf - pyproject.toml uv.lock src claude-gateway.service hardening.conf |
  remote incus exec "$NAME" -- tar -C /opt/claude-gateway --no-same-owner -xf -
# Install the exact locked dependency versions into /opt/claude-gateway/.venv,
# using the container's system Python (no interpreter download).
in_ct_sh "cd /opt/claude-gateway &&
  UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never uv sync -q --frozen --no-dev --no-editable --python /usr/bin/python3 &&
  ln -sf /opt/claude-gateway/.venv/bin/claude-gateway /usr/local/bin/claude-gateway &&
  install -m 0644 claude-gateway.service /etc/systemd/system/claude-gateway.service &&
  install -D -m 0644 hardening.conf /etc/systemd/system/claude-gateway.service.d/zzzz-hardening.conf &&
  systemctl daemon-reload && systemctl enable -q claude-gateway && systemctl restart claude-gateway"

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
