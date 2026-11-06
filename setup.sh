#!/bin/sh
# Runs inside the container, as root, on every deploy (deploy.sh calls it).
# Each step skips work that is already done, so a failed or partial run can
# simply be run again.
set -eu
cd /opt/claude-gateway

if ! command -v curl >/dev/null || ! command -v python3 >/dev/null; then
  echo "==> Installing Python and curl"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 curl ca-certificates >/dev/null
fi
if ! command -v tailscale >/dev/null; then
  echo "==> Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
fi
if ! command -v uv >/dev/null; then
  echo "==> Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
id claude-gateway >/dev/null 2>&1 ||
  useradd --system --home-dir /var/lib/claude-gateway --shell /usr/sbin/nologin claude-gateway

echo "==> Installing the gateway"
# The exact locked dependency versions, on the container's own Python.
UV_LINK_MODE=copy uv sync -q --frozen --no-dev --no-editable --python /usr/bin/python3
ln -sf /opt/claude-gateway/.venv/bin/claude-gateway /usr/local/bin/claude-gateway
install -m 0644 claude-gateway.service /etc/systemd/system/
install -D -m 0644 hardening.conf /etc/systemd/system/claude-gateway.service.d/zzzz-hardening.conf
systemctl daemon-reload
systemctl enable -q claude-gateway
systemctl restart claude-gateway

if ! health=$(curl -fs --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8080/_gateway/health); then
  echo "The service failed to start:" >&2
  journalctl -u claude-gateway -n 20 --no-pager >&2
  exit 1
fi
echo "==> Gateway running: $health"
