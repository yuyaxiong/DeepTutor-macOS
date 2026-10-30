#!/usr/bin/env python3
"""Start DeepTutor for the macOS app bundle.

The Swift launcher runs this file with the bundled Python runtime. It:
- picks free loopback ports,
- writes them to the app's runtime settings,
- publishes the frontend URL for the native window,
- replaces itself with the regular ``deeptutor start`` supervisor.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RESOURCES = Path(__file__).resolve().parent
SUPPORT = Path.home() / "Library" / "Application Support" / "DeepTutor"
SETTINGS = SUPPORT / "data" / "user" / "settings"
RUNTIME_DIR = SUPPORT / ".runtime"
RUNTIME_FILE = RUNTIME_DIR / "launch.json"
NODE_BIN = RESOURCES / "node" / "bin"

DEFAULT_BACKEND_PORT = 4782
DEFAULT_FRONTEND_PORT = 4783
PORT_SEARCH_SPAN = 100


def _port_is_free(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return False
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _pick_port(start: int, excluded: set[int]) -> int:
    for candidate in range(start, start + PORT_SEARCH_SPAN):
        if candidate in excluded:
            continue
        if _port_is_free(candidate):
            return candidate
    raise RuntimeError(f"No free port found in {start}..{start + PORT_SEARCH_SPAN - 1}")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _existing_instance() -> str | None:
    try:
        payload = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = payload.get("url")
    if not isinstance(url, str):
        return None
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/chat", timeout=1.5) as response:
            if 200 <= int(response.status) < 400:
                return url
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return None


def _load_settings() -> dict:
    try:
        payload = json.loads((SETTINGS / "system.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    SUPPORT.mkdir(parents=True, exist_ok=True)
    SETTINGS.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    running_url = _existing_instance()
    if running_url is not None:
        print(f"DeepTutor is already running at {running_url}", flush=True)
        return 0

    system = _load_settings()
    backend = system.get("backend_port")
    frontend = system.get("frontend_port")
    backend = int(backend) if isinstance(backend, int) and _port_is_free(backend) else None
    frontend = int(frontend) if isinstance(frontend, int) and _port_is_free(frontend) else None

    if backend is None:
        backend = _pick_port(DEFAULT_BACKEND_PORT, {frontend} if frontend else set())
    if frontend is None or frontend == backend:
        frontend = _pick_port(DEFAULT_FRONTEND_PORT, {backend})

    system["backend_port"] = backend
    system["frontend_port"] = frontend
    # Let the launcher derive the browser-facing API base from the selected port.
    system["next_public_api_base"] = ""
    system["next_public_api_base_external"] = ""
    _write_json(SETTINGS / "system.json", system)

    url = f"http://127.0.0.1:{frontend}"
    _write_json(
        RUNTIME_FILE,
        {
            "url": url,
            "backend_port": backend,
            "frontend_port": frontend,
            "pid": os.getpid(),
            "started_at": int(time.time()),
        },
    )

    environment = os.environ.copy()
    environment["DEEPTUTOR_HOME"] = str(SUPPORT)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PATH"] = f"{NODE_BIN}:{environment.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}"
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)

    command = [
        sys.executable,
        "-m",
        "deeptutor_cli.main",
        "start",
        "--home",
        str(SUPPORT),
        "--no-browser",
    ]
    print(f"Starting DeepTutor: backend={backend} frontend={frontend}", flush=True)
    os.execve(sys.executable, command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
