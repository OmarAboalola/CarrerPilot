"""One-command, project-local setup and loopback launch."""

import argparse
import hashlib
import os
import socket
import subprocess
import sys
import time
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENVIRONMENT = ROOT / ".venv"
PYTHON = ENVIRONMENT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = ROOT / "requirements.lock.txt"


def main():
    parser = argparse.ArgumentParser(description="Set up and run CareerPilot locally.")
    parser.add_argument("--live", action="store_true", help="Use your local .env and private data/.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check", action="store_true", help="Install/check dependencies, then exit.")
    args = parser.parse_args()
    if sys.version_info < (3, 12):  # noqa: UP036 -- this launcher must explain an unsupported interpreter
        parser.exit(1, "Python 3.12+ is required. On Windows, try: py -3.12 run.py\n")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.environ.setdefault("CAREERPILOT_SETUP_STARTED", str(time.time()))

    if Path(sys.executable).resolve() != PYTHON.resolve():
        if not PYTHON.exists():
            print("Creating the project-local .venv...", flush=True)
            venv.EnvBuilder(with_pip=True).create(ENVIRONMENT)
        valid = subprocess.run(
            [str(PYTHON), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"],
            check=False,
        )
        if valid.returncode:
            parser.exit(1, "The existing .venv uses an older Python. Recreate it with Python 3.12+.\n")
        return subprocess.call([str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    marker = ENVIRONMENT / ".careerpilot-requirements.sha256"
    probe = subprocess.run(
        [sys.executable, "-c", "import uvicorn, fastapi, sqlalchemy, alembic, pypdf, pdfplumber, "
         "googleapiclient, google_auth_oauthlib, cryptography, httpx, multipart, itsdangerous, pydantic_settings"],
        capture_output=True,
        check=False,
    )
    if probe.returncode or not marker.exists() or marker.read_text().strip() != digest:
        print("Installing pinned runtime dependencies (first run only)...", flush=True)
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)
        ])
        marker.write_text(digest, encoding="utf-8")
    elapsed = time.time() - float(os.environ["CAREERPILOT_SETUP_STARTED"])
    print(f"Environment ready in {elapsed:.1f} seconds.", flush=True)
    if args.check:
        return 0

    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
        except OSError:
            parser.exit(1, f"Port {args.port} is unavailable. Stop its server or use --port 8766.\n")
    os.chdir(ROOT)
    os.environ["MODE"] = "live" if args.live else "fixture"
    os.environ["DATA_DIR"] = str(ROOT / ("data" if args.live else ".demo-data"))
    os.environ["BASE_URL"] = f"http://127.0.0.1:{args.port}"
    if not args.live:
        for name in ["LLM_API_KEY", "LLM_MODEL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "JSEARCH_API_KEY"]:
            os.environ[name] = ""
        os.environ["JSEARCH_ENABLED"] = "false"
    print(f"CareerPilot {'live' if args.live else 'demo'}: http://127.0.0.1:{args.port} (Ctrl+C to stop)", flush=True)
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}. Check your Python version and internet connection, then retry.", file=sys.stderr)
        raise SystemExit(1) from error
