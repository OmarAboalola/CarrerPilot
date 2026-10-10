"""Keep regression tests independent of local credentials, data and providers."""
import os
import shutil
import socket
import tempfile
from pathlib import Path

from app.config import Settings

_test_data = Path(tempfile.mkdtemp(prefix="careerpilot-regression-"))
os.environ.update(DATA_DIR=str(_test_data), MODE="fixture", LLM_API_KEY="", LLM_MODEL="",
                  GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="", JSEARCH_API_KEY="", JSEARCH_ENABLED="false")
Settings.model_config["env_file"] = None
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _offline(sock, address):
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "localhost", "::1"}:
        return _connect(sock, address)
    raise AssertionError("Mock external network access in regression tests.")


def _offline_ex(sock, address):
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "localhost", "::1"}:
        return _connect_ex(sock, address)
    raise AssertionError("Mock external network access in regression tests.")


socket.socket.connect = _offline
socket.socket.connect_ex = _offline_ex


def pytest_sessionfinish(session, exitstatus):
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    # Only the directory created by this test session is eligible for removal.
    if _test_data.resolve().parent == Path(tempfile.gettempdir()).resolve() and _test_data.name.startswith("careerpilot-regression-"):
        from app.main import app
        app.state.store.engine.dispose()
        shutil.rmtree(_test_data)
