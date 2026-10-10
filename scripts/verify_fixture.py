"""Measure synthetic runtime separately from user/SME impact."""

import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def main():
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Settings(mode="fixture", data_dir=Path(directory)))
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf"]
            profile = client.get("/api/profile/example").json()["profile"]
            client.post("/api/profile/confirm", json=profile).raise_for_status()
            start = time.perf_counter()
            scan = client.post("/api/scans", json={}).json()
            for _ in range(100):
                result = client.get("/api/scans/" + scan["id"]).json()
                if result["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            report = {
                "mode": "synthetic",
                "state": result["state"],
                "runtime_seconds": round(time.perf_counter() - start, 3),
                "jobs": len(result["result"]["jobs"]),
                "learning": [s["skill"] for s in result["result"]["market"]["learning"]],
                "not_sme_time_saved": True,
            }
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
