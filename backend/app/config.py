"""Settings read from environment variables (or a .env file next to the backend)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

# Kite tick/candle timestamps and market hours are IST. Run the whole process in IST so
# naive datetimes (datetime.now(), KiteTicker timestamps) mean Indian time on any server.
os.environ["TZ"] = "Asia/Kolkata"
if hasattr(time, "tzset"):
    time.tzset()


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    kite_api_key: str = os.environ.get("KITE_API_KEY", "")
    kite_api_secret: str = os.environ.get("KITE_API_SECRET", "")
    # Long random string; the mobile app sends it as "Authorization: Bearer <token>"
    app_token: str = os.environ.get("APP_TOKEN", "")
    # Deep link the server redirects to after Kite login (must match the mobile app scheme)
    app_redirect: str = os.environ.get("APP_REDIRECT", "kitealgo://login")
    data_dir: Path = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
    # Real orders are impossible unless this is true on the server.
    live_trading_enabled: bool = _bool("LIVE_TRADING_ENABLED", False)
    # Hard ceilings that apply to every live session regardless of what the app sends
    max_live_qty: int = int(os.environ.get("MAX_LIVE_QTY", "100"))
    max_live_daily_loss: float = float(os.environ.get("MAX_LIVE_DAILY_LOSS", "2000"))
    max_live_lots: int = int(os.environ.get("MAX_LIVE_LOTS", "1"))
    max_live_orders_per_day: int = int(os.environ.get("MAX_LIVE_ORDERS_PER_DAY", "20"))
    order_tag: str = os.environ.get("ORDER_TAG", "kitealgo")[:20]


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
