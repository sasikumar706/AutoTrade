"""Test environment, set before any app module is imported (settings are read at import)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["APP_TOKEN"] = "test-token-0123456789-abcdefghij"
os.environ.setdefault("KITE_API_KEY", "fakekey")
os.environ["LIVE_TRADING_ENABLED"] = "false"
os.environ["DATA_DIR"] = os.path.join(tempfile.mkdtemp(prefix="kitealgo-test-"), "data")


def pytest_sessionfinish(session, exitstatus):
    """Let background work (AI pick / Day Builder threads started by API tests) finish before Python shuts down —
    a thread still inside numpy/pandas at interpreter exit aborts the process ('terminate called…')."""
    import threading
    for t in threading.enumerate():
        if t is not threading.main_thread() and t.is_alive():
            t.join(timeout=60)
