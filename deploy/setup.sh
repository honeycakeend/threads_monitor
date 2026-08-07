#!/usr/bin/env bash
# Deploy / update the Threads Monitor bot on an Ubuntu VPS.
# Run as root:  sudo bash deploy/setup.sh   (or from the cloned repo)
set -euo pipefail

REPO_URL="https://github.com/honeycakeend/threads_monitor.git"
APP_DIR="/opt/threads_bot"
APP_USER="threadsbot"
SERVICE="threads-bot"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (use sudo)." >&2
  exit 1
fi

echo ">>> Installing system packages"
apt-get update
apt-get install -y git python3 python3-venv python3-pip software-properties-common

# The code uses enum.StrEnum, which requires Python >= 3.11.
has_py() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

if has_py python3; then
  PYTHON=python3
elif command -v python3.11 >/dev/null 2>&1 && has_py python3.11; then
  PYTHON=python3.11
else
  echo ">>> System Python is older than 3.11 — installing python3.11 from deadsnakes"
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update
  apt-get install -y python3.11 python3.11-venv
  PYTHON=python3.11
fi
echo ">>> Using interpreter: $PYTHON ($($PYTHON --version))"

echo ">>> Creating service user '$APP_USER'"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo ">>> Fetching source into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo ">>> Setting up virtualenv and dependencies"
"$PYTHON" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/data"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo ">>> Created $APP_DIR/.env — you MUST edit it and set credentials."
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo ">>> Installing systemd service"
cp "$APP_DIR/deploy/threads-bot.service" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "$SERVICE"

echo
echo "==================================================================="
echo "Setup complete."
echo "1) Edit config:   sudo nano $APP_DIR/.env"
echo "2) Start the bot: sudo systemctl start $SERVICE"
echo "3) Check logs:    sudo journalctl -u $SERVICE -f"
echo "==================================================================="
