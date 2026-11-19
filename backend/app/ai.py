"""Optional: a written analysis from Claude (Anthropic API).

Enabled only when ANTHROPIC_API_KEY is set in backend/.env. Without it, the app uses
analysis.rule_based_summary() instead, so the feature works at no cost.
Only computed numbers (indicator values, backtest stats) are sent — never your Kite keys.
"""
from __future__ import annotations

import json
import os

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ai_summary(symbol: str, interval: str, snap: dict, finder: dict | None) -> str:
    import httpx

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    data = {"symbol": symbol, "interval": interval, "technical_snapshot": snap, "strategy_finder": finder}
    prompt = (
        "You are a careful trading-education assistant for an Indian retail trader using Zerodha. "
        "Using ONLY the JSON below (technical indicators on the latest candle and a walk-forward "
        "backtest ranking where 'test' is unseen recent data), write a short analysis in simple English:\n"
        "1. Overall bias and market regime, citing 2-4 indicators.\n"
        "2. Which strategy and settings look most suitable and why (prefer robust ones that made money "
        "in both train and test; mention trade count and drawdown). Say clearly if none is reliable.\n"
        "3. What the best strategy says right now (BUY/SELL/HOLD/WAIT) and a sensible stop-loss idea "
        "based on the ATR volatility.\n"
        "4. Key risks.\n"
        "Do not invent numbers. Do not promise profits. Under 200 words. End with: "
        "'Educational analysis, not investment advice — paper-trade first.'\n\n"
        + json.dumps(data, default=str)
    )
    r = httpx.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": model, "max_tokens": 700, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
