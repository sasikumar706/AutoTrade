"""API tests with a fake Kite (no network). Run on the server after `pip install -r requirements.txt`."""
import os
import tempfile
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("DATA_DIR", os.path.join(tempfile.gettempdir(), "kitealgo-test-data"))
os.environ["APP_TOKEN"] = "test-token-0123456789-abcdefghij"
os.environ.setdefault("KITE_API_KEY", "fakekey")

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from tests.test_engine import intraday  # noqa: E402

H = {"Authorization": "Bearer " + os.environ["APP_TOKEN"]}
ROWS = intraday(20)
INST = {"instrument_token": 408065, "exchange": "NSE", "tradingsymbol": "INFY", "name": "INFOSYS",
        "instrument_type": "EQ", "segment": "NSE", "lot_size": 1, "tick_size": 0.05}


@pytest.fixture(autouse=True)
def fake_kite(monkeypatch):
    k = main.kite
    monkeypatch.setattr(k, "find_instrument", lambda ex, sym: dict(INST))
    monkeypatch.setattr(k, "search_instruments", lambda q, ex=None: [INST])
    monkeypatch.setattr(k, "session_info", lambda: {"user_id": "AB1234", "user_name": "Test", "login_time": "", "expires": ""})

    def hist(token, interval, start, end, continuous=False, oi=False):
        return [dict(r, date=r["date"].isoformat()) for r in ROWS if start <= r["date"] <= end]

    monkeypatch.setattr(k, "historical", hist)
    yield


client = TestClient(main.app)


def test_auth_required():
    assert client.get("/strategies").status_code == 401
    assert client.get("/strategies", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_strategies_and_search():
    r = client.get("/strategies", headers=H).json()
    assert {s["key"] for s in r} >= {"sma_cross", "rsi_reversion", "supertrend", "orb", "bollinger"}
    assert client.get("/instruments/search?q=INF", headers=H).json()[0]["tradingsymbol"] == "INFY"


def test_backtest_roundtrip():
    body = {"tradingsymbol": "INFY", "interval": "5minute", "from_date": "2026-03-01", "to_date": "2026-03-31",
            "strategy": "supertrend", "stop_loss_pct": 1, "target_pct": 2}
    r = client.post("/backtest", json=body, headers=H)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["metrics"]["trades"] > 0 and res["id"]
    assert client.get(f"/backtests/{res['id']}", headers=H).json()["metrics"] == res["metrics"]
    assert any(b["id"] == res["id"] for b in client.get("/backtests", headers=H).json())


def test_backtest_validation_error_is_400():
    body = {"tradingsymbol": "INFY", "interval": "5minute", "from_date": "2026-03-01", "to_date": "2026-03-31",
            "strategy": "sma_cross", "params": {"fast": 30, "slow": 10}}
    assert client.post("/backtest", json=body, headers=H).status_code == 400


def test_replay_session_runs_to_completion():
    body = {"mode": "replay", "tradingsymbol": "INFY", "strategy": "sma_cross", "quantity": 10,
            "replay_from": "2026-03-20", "replay_to": "2026-03-27"}
    r = client.post("/sessions", json=body, headers=H)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    for _ in range(200):
        snap = client.get(f"/sessions/{sid}", headers=H).json()
        if snap["status"] == "finished":
            break
        import time
        time.sleep(0.05)
    assert snap["status"] == "finished" and snap["candles_seen"] > 0
    assert client.get(f"/sessions/{sid}/events", headers=H).json()


def test_live_refused_when_disabled():
    body = {"mode": "live", "tradingsymbol": "INFY", "strategy": "sma_cross", "confirm_live": "I UNDERSTAND REAL MONEY"}
    assert client.post("/sessions", json=body, headers=H).status_code == 403


def test_analyze_rules_and_ai_fallback(monkeypatch):
    body = {"tradingsymbol": "INFY", "interval": "5minute", "from_date": "2026-03-01", "to_date": "2026-03-31"}
    r = client.post("/analyze", json=body, headers=H)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["snapshot"]["verdict"] in ("BULLISH", "BEARISH", "NEUTRAL")
    assert res["finder"]["ranking"] and res["summary_source"] == "rules"
    # with a key set but the AI call failing, the free summary is still returned
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(main.ai, "ai_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    res = client.post("/analyze", json={**body, "find": False}, headers=H).json()
    assert res["summary_source"] == "rules" and "offline" in res["ai_error"] and res["finder"] is None
    monkeypatch.setattr(main.ai, "ai_summary", lambda *a, **k: "AI text")
    assert client.post("/analyze", json={**body, "find": False}, headers=H).json()["summary"] == "AI text"



def test_analysis_history_keep_and_clear():
    client.delete("/analyses", headers=H)
    body = {"tradingsymbol": "INFY", "interval": "5minute", "from_date": "2026-03-01", "to_date": "2026-03-31", "find": False}
    a = client.post("/analyze", json=body, headers=H).json()
    assert a["id"] and a["index_key"] is None
    client.post("/analyze", json=body, headers=H)
    assert client.post("/analyze", json={**body, "save": False}, headers=H).json()["id"] is None
    rows = client.get("/analyses", headers=H).json()
    assert len(rows) == 2 and rows[0]["title"] == "NSE:INFY" and rows[0]["verdict"]
    assert client.get(f"/analyses/{a['id']}", headers=H).json()["snapshot"]["verdict"]
    client.delete(f"/analyses/{a['id']}", headers=H)
    assert len(client.get("/analyses", headers=H).json()) == 1
    assert client.delete("/analyses", headers=H).json()["deleted"] == 1
    assert client.get("/analyses", headers=H).json() == []


def test_index_moves():
    q = {"NSE:NIFTY 50": {"last_price": 24100.0, "ohlc": {"close": 24000.0, "open": 24010, "high": 24120, "low": 23990}},
         "NSE:NIFTY BANK": {"last_price": 51900.0, "ohlc": {"close": 52000.0}},
         "BSE:SENSEX": {"last_price": 80010.0, "ohlc": {"close": 80000.0}}}
    m = main.index_moves(q)
    assert m["NIFTY"]["change"] == 100.0 and m["NIFTY"]["change_pct"] == 0.42 and m["NIFTY"]["direction"] == "UP"
    assert m["BANKNIFTY"]["change"] == -100.0 and m["BANKNIFTY"]["direction"] == "DOWN"
    assert m["SENSEX"]["direction"] == "FLAT"
    assert main.index_moves({})["NIFTY"]["change"] is None


def test_index_option_mapping():
    assert main.index_key_for("NSE", "NIFTY 50") == "NIFTY"
    assert main.index_key_for("NSE", "NIFTY BANK") == "BANKNIFTY"
    assert main.index_key_for("BSE", "SENSEX") == "SENSEX"
    assert main.index_key_for("NFO", "BANKNIFTY26NOVFUT") == "BANKNIFTY"
    assert main.index_key_for("NFO", "NIFTYNXT5026NOVFUT") is None
    assert main.index_key_for("NSE", "INFY") is None
    assert main.option_side("BUY") == "CE" and main.option_side("HOLD SHORT") == "PE" and main.option_side("WAIT") is None


# ---------------------------------------------------------------- index options endpoints
@pytest.fixture
def opt_env(monkeypatch):
    from app.kite_service import KiteService
    from datetime import datetime as _dt
    import app.options_manager as om_mod
    from tests.test_options import DAY, INST, FakeKite, EXP1

    class FrozenNow(_dt):  # the test chain has fixed expiries, so pin "today" to the test day
        @classmethod
        def now(cls, tz=None):
            return _dt.combine(DAY, _dt.min.time()).replace(hour=10)

    monkeypatch.setattr(om_mod, "datetime", FrozenNow)
    fk = FakeKite()
    monkeypatch.setattr(main.kite, "instruments", lambda: INST)
    monkeypatch.setattr(KiteService, "kite", property(lambda self: fk))
    return fk, EXP1


def test_options_chain_quote_and_manual_paper_order(opt_env):
    fk, exp = opt_env
    info = client.get("/options/chain-info?index=NIFTY", headers=H).json()
    assert exp in info["expiries"] and info["step"] == 50
    q = client.post("/options/quote", json={"index": "NIFTY", "expiry": exp, "option_type": "PE", "strike_method": "OTM",
                                            "strike_offset": 2, "lots": 3}, headers=H).json()
    assert q["quantity"] == 195 and q["contract"]["strike"] == 23900 and q["preview"]["stop_loss"] < q["preview"]["entry"]
    assert q["margin_required"] == 7000.0
    body = {"index": "NIFTY", "expiry": exp, "option_type": "CE", "lots": 1, "mode": "paper"}
    r = client.post("/options/order", json=body, headers=H)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert client.post("/options/order", json=body, headers=H).status_code == 403  # duplicate contract
    assert client.post("/options/order", json={**body, "mode": "live", "confirm_live": "I UNDERSTAND REAL MONEY"}, headers=H).status_code == 403
    assert client.post("/options/order", json={**body, "side": "SELL"}, headers=H).status_code == 403  # selling not allowed
    closed = client.post(f"/positions/{pid}/close", headers=H).json()
    assert closed["state"] == "POSITION_CLOSED"
    assert client.get("/analytics", headers=H).json()["closed_trades"] >= 1
    days = client.get("/positions/days?mode=paper", headers=H).json()
    assert days and days[0]["trades"] >= 1 and "manual" in days[0]["by"]
    assert any(p["id"] == pid for p in client.get(f"/positions?mode=paper&day={days[0]['day']}", headers=H).json())
    assert client.delete("/positions/history?mode=paper&source=manual", headers=H).json()["deleted"] >= 1
    assert all(p["id"] != pid for p in client.get("/positions?mode=paper", headers=H).json())


def test_options_backtest_job_run_and_stop(opt_env):
    import time as _t
    body = {"index": "NIFTY", "interval": "5minute", "from_date": "2026-03-01", "to_date": "2026-03-31",
            "strategy": "sma_cross", "params": {"fast": 3, "slow": 8}}
    j = client.post("/options/backtest", json=body, headers=H).json()
    for _ in range(200):
        s = client.get(f"/jobs/{j['id']}", headers=H).json()
        if s["status"] != "running":
            break
        _t.sleep(0.05)
    assert s["status"] == "done", s
    assert s["result"]["metrics"]["trades"] > 0 and s["result"]["trades"][0]["qty"] == 65
    j2 = client.post("/options/backtest", json=body, headers=H).json()
    client.post(f"/jobs/{j2['id']}/cancel", headers=H)
    for _ in range(200):
        s2 = client.get(f"/jobs/{j2['id']}", headers=H).json()
        if s2["status"] != "running":
            break
        _t.sleep(0.05)
    assert s2["status"] in ("cancelled", "done")
    bad = client.post("/options/backtest", json={**body, "side": "SELL"}, headers=H)
    assert bad.status_code == 403


def test_auto_trade_lifecycle(opt_env):
    fk, exp = opt_env
    r = client.post("/auto", json={"index": "NIFTY", "lots": 1}, headers=H)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert client.post("/auto", json={"index": "NIFTY", "mode": "live"}, headers=H).status_code == 403
    assert client.post(f"/auto/{aid}/control", json={"action": "enable"}, headers=H).json()["enabled"] is True
    assert client.post(f"/auto/{aid}/control", json={"action": "pause"}, headers=H).json()["paused"] is True
    r2 = client.put(f"/auto/{aid}", json={"index": "BANKNIFTY"}, headers=H)  # paper: saved while running, restarts itself
    assert r2.status_code == 200 and r2.json()["config"]["index"] == "BANKNIFTY" and r2.json()["enabled"] is True
    snap = client.post(f"/auto/{aid}/control", json={"action": "disable"}, headers=H).json()
    assert snap["enabled"] is False
    assert client.put(f"/auto/{aid}", json={"index": "SENSEX"}, headers=H).json()["config"]["index"] == "SENSEX"
    st = client.get(f"/analytics?source=auto:{aid}", headers=H).json()
    assert st["settings_version"] >= 2 and "index" in st["versions"][-1]["changes"]
    assert "scorecard" in st and "tuning" in st and st["scorecard"]["ready"] is False
    assert client.post(f"/auto/{aid}/control", json={"action": "close_position"}, headers=H).status_code == 400
    assert any(a["id"] == aid for a in client.get("/auto", headers=H).json())
    assert client.delete(f"/auto/{aid}", headers=H).json()["ok"]


def test_clear_history_endpoints():
    r = client.delete("/positions/history?mode=paper&source=manual", headers=H)
    assert r.status_code == 200 and "deleted" in r.json()
    r = client.delete("/sessions/history?mode=paper", headers=H)
    assert r.status_code == 200 and "deleted" in r.json()


def test_session_export_import(monkeypatch):
    from app import main as m

    class FK:
        access_token = None
        def set_access_token(self, t): self.access_token = t
        def profile(self): return {"user_id": "AB1234", "user_name": "Tester"}

    monkeypatch.setattr(m.kite, "_new_client", lambda: FK())
    monkeypatch.setattr(m.manager.hub, "restart", lambda: None)
    r = client.post("/auth/session-import", json={"access_token": "tok123"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["session"]["user_id"] == "AB1234"
    ex = client.get("/auth/session-export", headers=H).json()
    assert ex["access_token"] == "tok123" and ex["user_id"] == "AB1234"
    assert client.get("/auth/session-export").status_code in (401, 403)


def test_token_error_clears_saved_session(monkeypatch):
    from app import main as m

    class TokenException(Exception):
        pass

    m.kite.store.set("kite_session", {"access_token": "x", "user_id": "U", "user_name": "N",
                                       "login_time": m.datetime.now().isoformat()})

    def boom():
        raise TokenException("Incorrect `api_key` or `access_token`.")
    with pytest.raises(m.HTTPException) as ei:
        m._kite_errors(boom)
    assert ei.value.status_code == 401 and m.kite.store.get("kite_session") is None


def test_report_link_and_account_day_endpoints():
    r = client.post("/report/link", json={"days": 30, "mode": "all", "source": "all"}, headers=H)
    assert r.status_code == 200, r.text
    links = r.json()
    html = client.get(links["html"])
    assert html.status_code == 200 and "<html" in html.text.lower()
    assert client.get(links["csv"]).status_code == 200
    assert client.post("/report/link", json={"days": 9999}, headers=H).status_code == 400
    assert client.get("/account/days", headers=H).status_code == 200
    assert client.get("/account/day?day=2020-01-01", headers=H).status_code == 404
    assert client.get("/events", headers=H).status_code == 200
    assert client.put("/events", json=[{"day": "2030-01-01", "event": "Test"}], headers=H).status_code == 200


def test_swing_endpoints_create_update_delete():
    r = client.post("/swing", json={"index": "NIFTY", "lots": 1}, headers=H)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["config"]["fast"] == 10 and r.json()["config"]["slow"] == 50 and not r.json()["enabled"]
    assert any(w["id"] == sid for w in client.get("/swing", headers=H).json())
    u = client.put(f"/swing/{sid}", json={"index": "NIFTY", "lots": 2, "check_time": "15:10"}, headers=H)
    assert u.status_code == 200 and u.json()["config"]["lots"] == 2
    assert client.put(f"/swing/{sid}", json={"fast": 60, "slow": 50}, headers=H).status_code == 400
    g = client.get(f"/swing/{sid}", headers=H).json()
    assert g["trades"] == [] and g["position"] is None
    assert client.post(f"/swing/{sid}/control", json={"action": "disable"}, headers=H).status_code == 200
    assert client.delete(f"/swing/{sid}", headers=H).status_code == 200
