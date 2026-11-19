#!/data/data/com.termux/files/usr/bin/bash
# Start the backend on the phone and RESTART it automatically if it ever stops.
# termux-wake-lock stops Android from sleeping the server while sessions run.
# Stop with Ctrl+C (or close Termux).
cd "$(dirname "$0")/.."
termux-wake-lock 2>/dev/null || true
mkdir -p data
echo "Server running at http://127.0.0.1:8000  (auto-restarts if it crashes; Ctrl+C to stop)"
trap 'echo "Stopping."; exit 0' INT TERM
while true; do
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  code=$?
  echo "$(date '+%F %T') server stopped (exit $code) — restarting in 5 s" | tee -a data/restarts.log
  command -v termux-notification >/dev/null && termux-notification --title "Kite Algo" --content "Phone server stopped (exit $code) and is restarting" --priority high >/dev/null 2>&1 || true
  if [ -n "$NTFY_TOPIC" ] || grep -q '^NTFY_TOPIC=.\+' .env 2>/dev/null; then
    topic=${NTFY_TOPIC:-$(grep '^NTFY_TOPIC=' .env | cut -d= -f2-)}
    curl -s -m 8 -H "Title: Kite Algo" -H "Priority: high" -d "Phone server stopped (exit $code) and is restarting" "https://ntfy.sh/$topic" >/dev/null 2>&1 || true
  fi
  sleep 5
done
