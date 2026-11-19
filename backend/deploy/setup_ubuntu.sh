#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 22.04/24.04 VM with a static public IP (e.g. AWS Mumbai + Elastic IP).
set -euo pipefail
sudo timedatectl set-timezone Asia/Kolkata
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo ">> Edit backend/.env now, then re-run the last steps"; }
.venv/bin/python -m pytest -q tests
sudo cp deploy/kitealgo.service /etc/systemd/system/
sudo sed -i "s#/home/ubuntu/kite-algo-app/backend#$(pwd)#g; s#User=ubuntu#User=$USER#" /etc/systemd/system/kitealgo.service
sudo systemctl daemon-reload && sudo systemctl enable --now kitealgo
echo ">> Now edit /etc/caddy/Caddyfile (see deploy/Caddyfile) and: sudo systemctl reload caddy"
echo ">> Check your public IP (register it in Kite developer console -> Profile -> IP Whitelist):"
curl -s https://checkip.amazonaws.com
