#!/bin/sh
# Runs inside the container, as root, on every deploy (deploy.sh calls it).
# Each step skips work that is already done, so a failed or partial run can
# simply be run again.
set -eu
cd /opt/hotpocket

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
id hotpocket >/dev/null 2>&1 ||
  useradd --system --home-dir /var/lib/hotpocket --shell /usr/sbin/nologin hotpocket

echo "==> Installing the gateway"
# The exact locked dependency versions, on the container's own Python.
UV_LINK_MODE=copy uv sync -q --frozen --no-dev --no-editable --python /usr/bin/python3
# `hotpocket` for admins: run as root (incus exec), it switches to the service user, so the
# database files it writes stay the service's.
rm -f /usr/local/bin/hotpocket
install -m 0755 hotpocket-cli.sh /usr/local/bin/hotpocket
install -m 0644 hotpocket.service /etc/systemd/system/
install -D -m 0644 hardening.conf /etc/systemd/system/hotpocket.service.d/zzzz-hardening.conf
systemctl daemon-reload
systemctl enable -q hotpocket
systemctl restart hotpocket

if ! curl -fs -o /dev/null --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8080/api/hello; then
  echo "The service failed to start:" >&2
  journalctl -u hotpocket -n 20 --no-pager >&2
  exit 1
fi
echo "==> Gateway running"
