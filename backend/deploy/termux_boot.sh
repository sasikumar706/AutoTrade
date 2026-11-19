#!/data/data/com.termux/files/usr/bin/bash
# Start the Kite Algo server when the phone boots.
# One-time setup:
#   1. Install "Termux:Boot" (same store you got Termux from: F-Droid or GitHub) and open it once.
#   2. Run:  bash ~/kite-algo-app/backend/deploy/termux_boot.sh install
#   3. Android Settings → Apps → Termux → Battery → "Unrestricted" (so Android doesn't kill it).
if [ "$1" = "install" ]; then
  mkdir -p ~/.termux/boot
  cp "$0" ~/.termux/boot/start-kitealgo.sh
  chmod +x ~/.termux/boot/start-kitealgo.sh
  echo "Installed. The server will start by itself after every reboot."
  exit 0
fi
termux-wake-lock 2>/dev/null || true
cd ~/kite-algo-app/backend && nohup bash deploy/termux_start.sh >> data/server.log 2>&1 &
