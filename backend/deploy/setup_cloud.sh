#!/usr/bin/env bash
# One-time setup of the Kite Algo server on a fresh Ubuntu cloud machine with a STATIC public IP
# (e.g. AWS Lightsail Mumbai + static IP). Run from the repo:  bash backend/deploy/setup_cloud.sh
#
# What it does: Indian time zone, 1 GB swap, Python + packages, backend/.env (asks for your Kite keys),
# a service that starts on boot and restarts on crash, and Tailscale so ONLY your phone can reach it
# (port 8000 is never opened to the internet). Real-money orders stay OFF until you switch them on.
set -euo pipefail
cd "$(dirname "$0")/.."
BACKEND=$(pwd)

echo "== 1/6 Time zone and swap"
sudo timedatectl set-timezone Asia/Kolkata
if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== 2/6 Python packages (takes a few minutes)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip curl
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== 3/6 Settings (backend/.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  read -rp "Kite API key (same as on your phone): " KEY
  read -rp "Kite API secret (same as on your phone): " SECRET
  read -rp "ntfy topic for phone alerts (Enter to skip): " TOPIC
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  sed -i "s#^KITE_API_KEY=.*#KITE_API_KEY=$KEY#; s#^KITE_API_SECRET=.*#KITE_API_SECRET=$SECRET#; s#^APP_TOKEN=.*#APP_TOKEN=$TOKEN#" .env
  sed -i "s#^LIVE_TRADING_ENABLED=.*#LIVE_TRADING_ENABLED=false#" .env
  if [ -n "$TOPIC" ]; then
    if grep -q '^NTFY_TOPIC=' .env; then sed -i "s#^NTFY_TOPIC=.*#NTFY_TOPIC=$TOPIC#" .env; else echo "NTFY_TOPIC=$TOPIC" >> .env; fi
  fi
else
  echo "backend/.env already exists — keeping it"
fi

echo "== 4/6 Service (starts on boot, restarts on crash)"
sudo tee /etc/systemd/system/kitealgo.service >/dev/null <<EOF
[Unit]
Description=Kite Algo backend
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$BACKEND
ExecStart=$BACKEND/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5
Environment=TZ=Asia/Kolkata

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now kitealgo
sleep 3
curl -s -m 5 http://127.0.0.1:8000/health && echo

echo "== 5/6 Tailscale (private link between this server and your phone)"
if ! command -v tailscale >/dev/null; then curl -fsSL https://tailscale.com/install.sh | sh; fi
echo ">> Open the link printed below on your phone and log in with the SAME account you use in the Tailscale app:"
sudo tailscale up
TS_IP=$(tailscale ip -4 | head -1)

echo "== 6/6 Done"
PUBLIC_IP=$(curl -s -m 5 https://checkip.amazonaws.com || echo "?")
APP_TOKEN=$(grep '^APP_TOKEN=' .env | cut -d= -f2-)
cat <<EOF

=====================================================================
 Static public IP (whitelist this in Kite):  $PUBLIC_IP
 In the Kite Algo app → Settings → PC server:
     URL:    http://$TS_IP:8000
     Token:  $APP_TOKEN
 Real-money orders: OFF (LIVE_TRADING_ENABLED=false in backend/.env)
=====================================================================
Useful commands:
  sudo systemctl restart kitealgo        # restart after changing .env
  sudo journalctl -u kitealgo -n 50      # see the server log
  cd ~/kite-algo-app && git pull && sudo systemctl restart kitealgo   # update
EOF
