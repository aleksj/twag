#!/usr/bin/env bash
set -euo pipefail

# Run this on the Ubuntu host after rsync has copied the repository.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
ENV_DIR="${ENV_DIR:-/etc/twag}"
ENV_FILE="$ENV_DIR/twag.env"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer is intended for Ubuntu/Linux hosts." >&2
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required." >&2
  exit 1
fi

echo "Installing TWAG from: $APP_DIR"
echo "Service user: $SERVICE_USER"

case "$APP_DIR" in
  /tmp/*|/var/tmp/*)
    cat >&2 <<EOF
Warning: $APP_DIR is temporary storage. Use /home/$SERVICE_USER/twag or /opt/twag
for persistent systemd services; otherwise a reboot or tmp cleanup can remove the
application directory and systemd will fail before the process starts.
EOF
    ;;
esac

sudo install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

sudo install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  sudo install -m 0640 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
    "$APP_DIR/deploy/ubuntu/twag.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE from deploy/ubuntu/twag.env.example"
  echo "Edit it before starting services."
else
  echo "$ENV_FILE already exists; leaving it untouched."
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

for unit in twag-telegram-agent@.service twag-nimble@.service; do
  sed "s#__APP_DIR__#$APP_DIR#g" "$APP_DIR/deploy/ubuntu/$unit" > "$tmpdir/$unit"
  sudo install -m 0644 "$tmpdir/$unit" "$SYSTEMD_DIR/$unit"
done

sudo systemctl daemon-reload

cat <<EOF

Installed systemd units:
  twag-telegram-agent@.service
  twag-nimble@.service

Edit secrets:
  sudoedit $ENV_FILE

Start both services:
  sudo systemctl enable --now twag-telegram-agent@$SERVICE_USER.service
  sudo systemctl enable --now twag-nimble@$SERVICE_USER.service

Logs:
  journalctl -u twag-telegram-agent@$SERVICE_USER.service -f
  journalctl -u twag-nimble@$SERVICE_USER.service -f

EOF
