#!/data/data/com.termux/files/usr/bin/bash
# Run the backend ON YOUR ANDROID PHONE inside Termux (install Termux from F-Droid, not Play Store).
# Phone mode supports: login, backtests, replay and PAPER trading.
# LIVE orders will be rejected by Zerodha: a phone has no static IP to whitelist.
#
#   pkg install -y git && git clone https://github.com/sasikumar706/kite-algo-app
#   bash kite-algo-app/backend/deploy/termux_setup.sh
set -e
cd "$(dirname "$0")/.."

echo ">> Installing system packages (first time can take 15-30 minutes)"
pkg update -y
pkg install -y python python-numpy python-cryptography rust binutils clang make
pkg install -y tur-repo || true
pkg install -y python-pandas || true
pkg install -y python-pydantic-core || true
pkg install -y termux-api || true  # phone notifications (also install the Termux:API app)

# maturin (used to build pydantic-core if no prebuilt package) needs this on Android
export ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)"

echo ">> Installing Python packages"
python -c "import pandas" 2>/dev/null || pip install pandas
pip install fastapi uvicorn "kiteconnect>=5.2.2" pytest httpx

if [ ! -f .env ]; then
  cp .env.example .env
  TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  read -rp "Kite API key: " KEY
  read -rp "Kite API secret: " SECRET
  sed -i "s|^KITE_API_KEY=.*|KITE_API_KEY=$KEY|; s|^KITE_API_SECRET=.*|KITE_API_SECRET=$SECRET|; s|^APP_TOKEN=.*|APP_TOKEN=$TOKEN|" .env
  sed -i "s|^LIVE_TRADING_ENABLED=.*|LIVE_TRADING_ENABLED=false|" .env
  chmod 600 .env
fi

echo ">> Running tests"
python -m pytest -q tests

echo
echo "======================================================================"
echo " Setup done. In the Kite developer console set:"
echo "   Redirect URL : http://127.0.0.1:8000/auth/callback"
echo "   Postback URL : (leave empty)"
echo " In the Kite Algo app -> Settings:"
echo "   Server URL   : http://127.0.0.1:8000"
echo "   App token    : $(grep '^APP_TOKEN=' .env | cut -d= -f2-)"
echo " Start the server any time with:  bash backend/deploy/termux_start.sh"
echo "======================================================================"
