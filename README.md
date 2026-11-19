# Kite Algo — backtest, paper-trade and live-trade from your phone (Zerodha Kite Connect)

Personal-use app. Two parts:

| Part | What it does | Where it runs |
|---|---|---|
| `backend/` (Python, FastAPI) | Holds your Kite **api_secret** and daily **access_token**, downloads candles, runs backtests, runs paper / replay / live sessions 24×7, places orders | A small Linux server with a **static public IP** (needed by Zerodha for API orders since 1 Apr 2026) |
| `mobile/` (Expo React Native) | Remote control: login, backtest form + results, start/stop sessions, live P&L, kill switch | Your Android/iOS phone |

```
Phone app ──HTTPS + APP_TOKEN──▶ Your server (static IP) ──▶ Kite Connect API / WebSocket
            ◀── results ──────                       ◀── ticks, order updates
```

The phone never sees your API secret. Strategies keep running on the server when the phone is off.

---

## 1. What you need

1. **Zerodha account** + **Kite Connect "Connect" plan — ₹500/month per app** (developers.kite.trade). The free *Personal* plan has **no historical or live data**, so it can't backtest or paper-trade.
2. **A server with a static IP.** Example: AWS Lightsail/EC2 in Mumbai (`ap-south-1`) with an Elastic/static IP, or any Indian VPS. 1 vCPU / 1 GB RAM is enough.
3. **A domain or subdomain** (e.g. `algo.yourname.in`) pointing to that IP — needed for HTTPS and the Kite redirect URL.
4. Node.js 20+ on your computer for building the app.

## 2. Create the Kite Connect app

developers.kite.trade → **My Apps → Create new app**
- **Redirect URL:** `https://algo.yourname.in/auth/callback`
- **Postback URL (optional):** `https://algo.yourname.in/postback`
- Note the **API key** and **API secret**.

**Profile → IP Whitelist:** add your server's public IP (primary). You can change it only once a week. Data and WebSocket work from any IP, but **orders are rejected from any other IP**.

## 3. Set up the server

```bash
# on the server (Ubuntu 22.04/24.04)
git clone <your repo> kite-algo-app      # or upload the zip and unzip
cd kite-algo-app/backend
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> paste as APP_TOKEN
nano .env                                  # fill KITE_API_KEY, KITE_API_SECRET, APP_TOKEN
bash deploy/setup_ubuntu.sh                # venv, deps, tests, systemd service, Caddy
sudo nano /etc/caddy/Caddyfile             # put your domain (see deploy/Caddyfile)
sudo systemctl reload caddy
curl https://algo.yourname.in/health       # -> {"ok": true, ...}
```

The setup script runs the test suite (`pytest`) before starting the service — all tests must pass.

Keep `LIVE_TRADING_ENABLED=false` until you have paper-traded for a few weeks. The server's caps (`MAX_LIVE_QTY`, `MAX_LIVE_DAILY_LOSS`, `MAX_LIVE_ORDERS_PER_DAY`) apply to every live session and the app cannot override them.

Run only **one** uvicorn worker (already set) — sessions live in that process's memory.

## 4. Build the mobile app

```bash
cd mobile
npm install
npx expo install --fix        # aligns package versions with your Expo SDK
npx expo run:android          # builds & installs on a USB-connected phone/emulator
# or a shareable APK:  npm i -g eas-cli && eas build -p android --profile preview
```

Use a development/standalone build (not Expo Go) so the `kitealgo://` login redirect returns to the app. If it ever doesn't, just close the browser after logging in and pull to refresh — the server has already saved the session.

In the app: **Settings** → server URL + APP_TOKEN → *Save & test*. Then **Home → Login with Kite**.

## 5. Daily routine

1. **Every morning (after 6 AM):** Home → *Login with Kite*. Zerodha/exchange rules require a manual login each day; tokens expire at 6 AM. Don't automate the login with TOTP scripts.
2. Start paper (or live) sessions before 9:15 or anytime during market hours.
3. Intraday sessions square off at 15:10 (before Zerodha's own auto square-off, which charges ₹50 + GST per order), no new entries after 14:45.

## 6. Features

**Backtest** — any NSE/BSE/NFO/BFO instrument, 1m–60m or daily candles, 5 built-in strategies (EMA crossover, RSI reversion, Supertrend, Opening Range Breakout, Bollinger reversion), stop-loss/target %, % of capital or fixed quantity (lot sizes respected), MIS / CNC / NRML. Reports net P&L after **all Zerodha charges** (brokerage, STT, exchange, SEBI, stamp duty, GST, DP), win rate, profit factor, max drawdown, Sharpe/Sortino, CAGR, expectancy, equity + drawdown charts and every trade. Results are saved.

Realism rules: signal at candle close → fill at **next** candle open; slippage on every fill; if a candle touches both stop and target, the **stop** is assumed; gaps fill at the open; MIS never held overnight.

**Replay** — feeds past days' candles tick-by-tick through the exact live engine. Use it on weekends to check a strategy behaves the same live as in the backtest (the test suite verifies the two engines produce the same trades).

**Paper** — live Kite WebSocket ticks → candles → same strategy → simulated fills with slippage. Zero risk.

**Live** — same engine, but real `MARKET` orders with `market_protection=-1` (mandatory since 1 Apr 2026), tagged `kitealgo`. A position is only recorded after Kite confirms the fill. If an **exit** order is rejected the session halts and tells you to check Kite. Per-session max trades/day and max loss/day, server-wide caps, and a **kill switch** that stops everything and squares off.

## 7. Add your own strategy

`backend/app/strategies.py` — subclass `Strategy`, return boolean `entry_long / exit_long / entry_short / exit_short` columns computed only from past data, add it to `REGISTRY`. It appears in the app automatically. Add it to the `test_signals_have_no_lookahead` check (automatic for everything in `REGISTRY`).

## 8. Keep these up to date

- `backend/app/charges.py` — STT and exchange charges change (last revised 1 Apr 2026, Budget 2026-27).
- F&O lot sizes and expiry days change often; the app reads lot size from Kite's daily instrument list, but old backtests of expired contracts are not supported by Kite's API (it only lists live contracts; expired futures have daily candles only via `continuous`).

## 9. Rules to respect (India, 2026)

- Every API order is an "algo" order. Self-built algos for **your own account** under **10 orders/sec** need no exchange registration. Kite also caps 400 orders/min, 5,000/day.
- Orders only from your whitelisted static IP (2 IPs max, shared only with spouse/dependent children/parents).
- **Don't give this app to other people to trade their accounts** — that makes you an "algo provider", which needs exchange empanelment. Ask Zerodha first.
- Nothing here is investment advice. Backtests overstate real results; paper-trade first and start live with tiny size.

## 10. API reference (server)

All need `Authorization: Bearer <APP_TOKEN>` except `/health`, `/auth/callback`, `/postback`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/auth/login-url`, `/auth/status` · POST `/auth/logout` | Kite session |
| GET | `/instruments/search?q=INFY` | Instrument lookup |
| GET | `/historical?exchange&tradingsymbol&interval&from_date&to_date` | Raw candles |
| GET | `/strategies` | Strategy list + parameter schema |
| POST | `/backtest` · GET `/backtests`, `/backtests/{id}` | Backtests |
| POST | `/sessions` · GET `/sessions`, `/sessions/{id}`, `/sessions/{id}/events` · POST `/sessions/{id}/stop` | Paper / replay / live |
| POST | `/kill` | Emergency stop all |
| GET | `/account/margins`, `/account/positions`, `/account/orders` | Read-only account |
| POST | `/charges` | Charges calculator |

Run tests: `cd backend && .venv/bin/python -m pytest -q`

## 11. Automatic APK builds (GitHub Actions)

Every push to `main` runs `.github/workflows/build.yml`: backend tests, a TypeScript check, then an Android APK build.
Download it on your phone from the repo's **Releases → apk-latest → kite-algo.apk** (or Actions → latest run → Artifacts).
It is debug-signed for testing: Android will ask you to allow "Install unknown apps". For the Play Store, build with EAS and a proper keystore.

## 12. Phone-only mode (no server, no domain) — backtest + paper trading

Run the backend inside **Termux** on the same Android phone as the app.
Works: login, backtests, replay, paper trading. **Does not work: live orders** — Zerodha only accepts orders from a whitelisted static IP, and mobile data has none.

1. Install **Termux from F-Droid** (the Play Store version is outdated).
2. In Termux:
   ```bash
   pkg install -y git gh
   gh auth login                 # the repo is private: log in via browser
   gh repo clone sasikumar706/kite-algo-app
   bash kite-algo-app/backend/deploy/termux_setup.sh    # asks for Kite API key + secret
   ```
   It prints your **App token** at the end.
3. Kite developer console → your app → **Redirect URL** `http://127.0.0.1:8000/auth/callback`, **Postback URL** empty.
4. Start the server (every time): `bash kite-algo-app/backend/deploy/termux_start.sh` — keep Termux running in the background.
5. Kite Algo app → Settings → `http://127.0.0.1:8000` + the App token → Save & test → Home → Login with Kite.

When you move to a real server later, change the Redirect URL back to your https domain.

## 13. AI Strategy Finder (AI tab)

Pick an instrument + timeframe → **Analyse with AI**:
- **Market view:** 9 indicators (EMA 20/50/200, RSI, MACD, ADX, Supertrend, Bollinger %B, 20-bar range, volume) each vote bullish/bearish with a reason → verdict and score (-100…+100), trending/ranging regime, ATR volatility.
- **Best strategies:** every strategy × a grid of settings (~100 combinations) is tuned on the older 70% of history and **ranked only on the newer 30% it never saw** (walk-forward), with Zerodha charges included. Each shows what it says now (BUY / SELL / HOLD / WAIT / EXIT) with one-tap **Backtest** or **Paper trade**.
- **Summary:** built-in plain-English summary (free). Optional: set `ANTHROPIC_API_KEY` in `backend/.env` for a Claude-written analysis (only indicator numbers and backtest stats are sent — never Kite keys).

Server endpoint: `POST /analyze`. Educational analysis only — not investment advice.

## 14. Index options: NIFTY, BANKNIFTY, SENSEX (Backtest / Paper / Live / Auto Trade tabs)

**Contracts:** expiries, strikes and lot sizes come from Zerodha's daily instrument list (never hard-coded). Pick CE/PE,
ATM / ITM n / OTM n / exact strike, and number of lots; quantity = lots × current lot size. The preview recalculates premium,
bid/ask spread, order value, margin (Kite margin API), stop-loss, target, max loss, risk:reward and charges on every change.
BANKNIFTY has monthly expiries only (weeklies discontinued in 2025); NIFTY weekly (Tuesday); SENSEX weekly (Thursday, BFO).

**Mandatory risk:** every paper/live/auto trade needs a valid stop-loss (% / points / price on the premium). Targets: R:R
(default 1:2.1 — a risk-to-reward ratio, not a win rate), %, points, price or none. Optional trailing stop that only tightens.
Optional max loss per trade.

**Backtest → Index options:** BUY (buy to open) or SELL (sell to open, needs permission) · RUN / STOP / RESET · signals on
the index, trades in options · premium source: *Estimated* (Black-Scholes, long history) or *Real* (candles of a listed
expiry; Kite has no expired-contract data, so recent weeks only) · entry window (default 09:30–10:30), exit cutoff (10:30),
max trades/day, max loss/day · results: wins/losses, gross profit/loss, net after costs, profit factor, realised vs planned
R:R, drawdown, exit reasons, equity curve, every trade, skipped signals.

**Paper / Live tabs:** manual option orders with the same picker + risk form. A position in a contract must be closed
("SELL to close" / "BUY to close") before another order in it — no accidental reversals. LIVE: MARKET entry with
market protection, then a broker-side protective SL order; target/cutoff/manual exits cancel the SL then exit; nothing is
assumed filled until Kite confirms; rejected/partial fills and failed exits are tracked and retried; uncertain submissions
are reconciled with Kite's order book instead of being re-sent. Live tab also shows all Kite positions.

**Auto Trade tab:** per strategy: PAPER or LIVE, index, CE/PE rule, strike rule, expiry (nearest, skipping expiry day),
lots, strategy (default *Morning Momentum*: EMA20/50 trend + RSI + VWAP/TWAP + previous-day pivot breakout), mandatory SL,
target, windows (analysis 09:15–09:30 no orders; entries 09:30–10:30; everything closed at 10:30), max trades/day (2),
max loss/day, max capital/trade, max spread, min confidence, skip big gap days. Controls: ENABLE (confirmation; LIVE asks
again), PAUSE (no new entries), RESUME, DISABLE (no entries + cancel pending entry), CLOSE POSITION, KILL SWITCH.
State machine: IDLE → ANALYZING → SIGNAL_READY → RISK_VALIDATION → ENTRY_PENDING → POSITION_OPEN → EXIT_PENDING →
POSITION_CLOSED (+ SIGNAL_REJECTED, ORDER_REJECTED, PARTIALLY_FILLED, EXIT_FAILED, RECONCILIATION_REQUIRED,
SESSION_COMPLETED). Everything is saved in SQLite; after a restart positions are restored and LIVE ones reconciled with
Kite; strategies stay disabled until you re-enable them. Holidays: NSE 2026 list + `data/holidays.txt` for extra closures.
Dashboard: state, signal JSON (contract, entry, SL, target, qty, R:R, max loss, charges, confidence, reasons, validity),
previous-day levels and opening gap, P&L, remaining risk budget, dry-run preview, win rate, profit factor, signal→order
conversion, order acceptance, fill rate, slippage, rejected orders, stop/target execution.

**Limits of phone-only mode:** LIVE (manual or auto) needs the static-IP server; on the phone use PAPER.

**Tests:** `backend/tests/test_options.py` covers the 20 scenarios in the spec (previous-day data, ticks, CE/PE selection,
strike/expiry changes, lot sizes, SL validation, targets, R:R, backtest BUY/SELL, paper entry/exit, duplicates, partial/
rejected fills, SL/target triggers, analysis and entry windows, 10:30 exit, network failures, restart recovery, daily loss
limit, holidays).

## 13. Modes, AI history and AI → option trades
- **Mode switch** (Home and Trade tab): Backtest · Paper · Live. Home and the Trade tab show only that mode's tools; the choice is remembered.
- **Past analyses**: each AI Finder run is saved (latest 50) unless "Save to past analyses" is off. Open, delete one, or *Clear all*. API: `GET/DELETE /analyses`, `GET/DELETE /analyses/{id}`.
- **CE/PE ideas for indexes**: analyse NIFTY 50, NIFTY BANK or SENSEX (or their futures) and each strategy's current signal maps to an option to **buy** — BUY/HOLD LONG → CE, SELL/HOLD SHORT → PE, WAIT/EXIT → no trade. "Paper CE/PE" or "Live CE/PE" switches mode and opens the option ticket pre-filled (ATM); nothing is placed until you confirm.
- **Beginner signal light** (index analyses): 🟢 all five checks agree (strategy signal, proven on unseen data, indicators, today's index move, analysis under 30 min old) → Paper and Live offered; 🟡 one check fails → paper only; 🔴 two or more fail or no signal → don't trade. Index tiles on Home and AI Finder show today's move in points and % (green up, red down, grey flat) from `GET /market/indices`.

## 14. Auto Trade: trading window, AI strategy pick, "why" for each order
- **When to trade**: presets Morning 9:30–10:30, Till noon, Afternoon 12:00–14:30, Full day 9:30–14:45 (closes by 15:10), or Custom (HH:MM). Exit must be 15:15 or earlier, before Zerodha's intraday square-off.
- **✦ AI picks the best** (`strategy_source: "ai"`): each trading morning (and on enable) the walk-forward finder ranks every strategy on the index's recent candles; the top *robust* one (profitable on unseen data, 3+ trades) trades that day. None robust → WAIT all day. "Re-pick now" control action: `repick`.
- **Why this order**: every signal carries a `why` list — strategy picked, the rule that fired, today's index move, each indicator check (✓/✗), the contract chosen, stop/target/max loss and any risk rule that blocked it. Shown on the Auto Trade detail and on the position screen.

## 15. Market tab
- **Major indices** (Nifty 50, Bank Nifty, Sensex, Fin Nifty, Midcap 100, IT, Auto, Pharma) with today's move in points and %, a one-line market mood, and India VIX as a fear gauge. `GET /market/overview`.
- **Search any stock or index** (NSE/BSE cash only: `GET /instruments/search?cash_only=true&q=`) → detail page: price and change, intraday 5-min chart vs yesterday's close, day range, volume/circuits for stocks, 52-week high/low, 1W–1Y returns and a plain-language daily trend. `GET /market/detail?exchange=&tradingsymbol=`. "Analyse with AI Finder" opens the finder with it selected.

## 16. Mode chooser on open; Auto Trade fixes
- The app asks **Backtest · Paper · Live · Auto Trade** every time it opens and shows only that mode's tabs (Market is shared; AI Finder in the manual modes). The mode chip in the header switches.
- Auto Trade: counter-trend strategies (RSI reversion, Bollinger) are no longer silently blocked by the trend-confidence filter; editing settings rebuilds candles at the new interval and re-picks the AI strategy on enable while keeping today's trade count and loss; strategy exits are logged with the rule and candle time.
- AI pick now scores strategies like **one option lot** (index units = lot × 0.5 delta, ₹150 flat round-trip cost per lot, 1 bp slippage) and only inside the Auto Trade window, instead of stock charges on a few index units (which rejected almost everything). The pick lists every strategy tested with ✓/✗ and the reason, plus the picked strategy's entry/exit rule. Paper only: `use_best_unproven` practises with the best untested-proof strategy.
- **What happens next** (Auto Trade, live): every ~5 s, using the candle still forming, the server shows the price and today's move, which way the six indicator checks lean (up x/6 vs down y/6), what the strategy is waiting for, the candle-close time when it will decide, and — refreshed every 30 s with a live quote — the exact order it would place if its rule fires (contract, entry, stop, target, qty, max loss, and any risk rule that would block it). It is a forecast only; orders still need a confirmed closed candle.
- Auto Trade strategy page is split into tabs — **Now** (live prediction, open position) · **AI pick** · **Orders** (each signal with reasons; *Clear order history*) · **Stats** · **Settings** (edit/delete) · **Log** (*Clear log*). Server actions `clear_signals` / `clear_log` keep positions, P&L and stats.
- The prediction card also shows the **chosen strike's live premium** (LTP, move since last refresh, change vs yesterday, bid/ask, day high/low), refreshed every ~5 s; the order preview (entry/stop/target) refreshes every 20 s.
- **Clear history**: Paper tab → "Clear paper trade history" (closed manual paper option trades, `DELETE /positions/history?mode=paper&source=manual`) or "Clear paper session history" (`DELETE /sessions/history?mode=paper`); Auto Trade list → "Clear Auto Trade history" (closed auto positions + every strategy's order history and log). Open positions, running sessions and strategies are never removed. Only the app's records are cleared — nothing at Zerodha.
- **Log out of Kite** sits at the bottom of Home, Backtest, Paper, Live and Auto Trade screens. "+ New Auto Trade strategy" is at the top of the Auto Trade list.
- **Entry price tolerance** (Auto Trade, default ₹1, editable under Limits and in the Autopilot form): the option is bought with a LIMIT order at the quote + that many rupees (sell: minus), paper fills never worse than it, cancelled if unfilled within the signal validity (90 s); 0 = market order. Breakout strategies also support an optional index-level near-miss (`level_tolerance_pct`, off by default). Manual Paper/Live tickets still use market orders.
- **Mid-day start / restart**: Auto Trade now loads today's completed candles from 09:15 (and seeds the candle still forming with its real open/high/low), so indicators, the opening range, VWAP and the strategy's current state reflect what already happened. Opening gap uses today's real open. After a server restart, enabling reloads candles instead of waiting ~60 new candles. The log shows a back-check of the signals the strategy gave earlier today (past signals are not traded late). The AI pick also scores on today's completed candles.
- **Why an expected order may not trigger**: the prediction card shows a red "can't place this order" box when a limit blocks it (max capital/trade, remaining daily risk). New: **Reduce lots automatically** (default on) lowers lots — never raises them — until the order fits; if even 1 lot doesn't fit, it stays blocked. **Join window** (default 15 min): a signal that fired a few minutes earlier (e.g. while enabling or while blocked) is joined once, unless the index has already moved more than 0.3% since the signal.

## 17. ✦ AI Autopilot (Auto Trade)
Create Auto Trade → **AI Autopilot**: you set only Paper/Live, max capital per trade, max loss per day, max lots and max trades per day. Each trading morning (and on enable) the AI walk-forward tests every strategy on NIFTY, BANKNIFTY and SENSEX with 5- and 15-min candles (scored like one option lot, inside 9:30–14:45, with a 1.5×ATR stop and 2× target), and trades the best robust combination. It sets the index, candle size, strategy + settings, BUY CE on up-signals / BUY PE on down-signals (options are only bought), ATM strike, nearest expiry, stop-loss in premium points (0.5 delta × 1.5 × ATR), target 2× the stop, and lots sized so each trade risks at most half the daily limit. "Autopilot decided today" on the AI pick tab lists every choice. On a phone the morning test of 6 combinations takes a few minutes; progress is shown.
- **Trend Pullback** strategy (8th in the library, also tested by the AI pick/Autopilot): joins a trend that is already running — in an up-trend (EMA fast > slow, ADX ≥ min) it waits for a dip back to the fast EMA or VWAP, then buys CE when a candle closes above the previous high; down-trend mirrored → PE. Exit on a close back through the slow EMA or a trend flip. No entries before 09:30.
- **Top-3 combo** (AI pick & Autopilot, on by default via `use_combo`): after ranking, the AI also walk-forward tests its top 2–3 proven strategies (same index + candle size) **together** — any one's signal opens a trade, it is held while at least one still agrees, and it exits when none do or one signals the opposite way. The combo is used only if it is itself profitable on unseen data and keeps ≥70% of the best single strategy's profit; otherwise the single best is used. One position at a time; daily trade/loss limits still apply. The order's reasons show which member fired.

## 18. Phone + PC servers (live orders from a static IP)
Run two copies of the backend with the **same Kite API key**:
- **Phone (Termux)** — backtests and paper trading, as before (`http://127.0.0.1:8000`, `LIVE_TRADING_ENABLED=false`).
- **Windows PC with a static public IP** — live orders. In PowerShell from the repo folder:
  `powershell -ExecutionPolicy Bypass -File backend\deploy\windows_setup.ps1`, then double-click `backend\deploy\start_server.bat`.
  The setup asks for the Kite key/secret, creates an app token, optionally enables real-money orders, opens port 8000 on private networks, and prints the LAN URL and the public IP to whitelist in the Kite developer console.

In the app → Settings: enter both servers. Backtest/Paper use the phone, **Live uses the PC**, Auto Trade uses whichever you choose. Log in to Kite on the phone as usual (the Kite redirect URL stays `http://127.0.0.1:8000/auth/callback`); the app copies the session to the PC (`GET /auth/session-export` → `POST /auth/session-import`, verified with Kite, app-token protected). `GET /server/info` shows the PC's public IP and live switch. Same Wi-Fi works out of the box; to reach the PC from elsewhere use Tailscale (private, encrypted) rather than opening the port to the internet. Logging out of Kite on either server ends the session on both.
- **AI forecast** (AI Finder): from the best proven strategy's current signal — "Expected UP/DOWN about N pts", target and stop prices (its average win/loss in index points on unseen data), typical hold time, win rate, average edge, and whether the indicators agree; then Paper/Live CE/PE (indexes) or Backtest/Paper (stocks).
- **Strong-trend override** (Auto Trade setting, default 6 of 6): when the strategy has no signal but at least N of the 6 indicator checks agree on a closed candle and none disagree, it enters in that direction (again only after that direction cools to ≤3/6 and rebuilds; inside the window, all limits apply). It exits when the trend fades (≤2/6 still agree or ≥4/6 turn against) or at stop/target/close time. Labelled in the order's reasons as not part of the tested results. Off = 0.
- **More entries (Auto Trade, on by default):** besides the AI's chosen strategy, all 8 strategies are watched with their best settings from today's test. **Any one strategy's fresh signal** opens a trade that follows that strategy's own exit rule; **strategy agreement** (default 3+) opens a trade when that many strategies point the same way (exits when fewer than 2 still agree). Re-arms after the count drops. One position at a time; all limits apply.

## 19. App layout (Nov 2026)
Modes are Paper · Live · Auto Trade (Backtest removed from the app). Zerodha login/logout lives only in **Settings → Zerodha**; Home shows a banner when Kite is logged out or the server is unreachable. Active Auto Trade strategies (ON or holding a trade) are listed first. Action buttons are icons with short captions (▶ Enable, ⏸ Pause, ⏹ Disable, 💰 Sell now, ↻ Re-pick, ✎ Edit, 🗑 Delete/Clear). Soft light theme with larger text.
- **More professional setups** in the library (11 total): **VWAP Bounce** (dip to the day's average price in a trend and bounce), **CPR Breakout** (first close outside yesterday's Central Pivot Range on narrow-CPR days), **Gap and Go** (a gap that holds breaks the first minutes' high/low). All are tested by the AI like the others.
- **Best stop/target numbers** (Autopilot): for the chosen strategy, 9 combinations (stop 1/1.5/2 × ATR, target 1/1.5/2 × the stop) are tested; the best on the tuning period (expectancy weighted towards win rate) is used only if it also made money on unseen data. The AI pick tab shows the full table.
