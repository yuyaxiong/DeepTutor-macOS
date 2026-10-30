#!/usr/bin/env python3
"""Start DeepTutor for the macOS app bundle.

The Swift launcher runs this file with the bundled Python runtime. It:
- validates an existing instance by app version, build id, process and health,
- stops a stale or unhealthy instance before starting a new one,
- picks free loopback ports,
- writes them to the app's runtime settings,
- publishes the frontend URL for the native window,
- replaces itself with the regular ``deeptutor start`` supervisor.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
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
BUILD_INFO_FILE = RESOURCES / "build-info.json"
NODE_BIN = RESOURCES / "node" / "bin"

DEFAULT_BACKEND_PORT = 4782
DEFAULT_FRONTEND_PORT = 4783
PORT_SEARCH_SPAN = 100


def _load_build_info() -> tuple[str, str]:
    try:
        payload = json.loads(BUILD_INFO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown", "unknown"
    return str(payload.get("app_version", "unknown")), str(payload.get("build_id", "unknown"))


APP_VERSION, BUILD_ID = _load_build_info()


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


def _read_runtime() -> dict | None:
    try:
        payload = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _pid_is_launcher(pid: int) -> bool:
    command = _pid_command(pid)
    return (
        ("deeptutor_cli.main" in command and " start" in command)
        or ("bootstrap.py" in command and "DeepTutor" in command)
    )


def _pid_is_old_backend(pid: int) -> bool:
    return "deeptutor.api.main" in _pid_command(pid)


def _pid_is_old_frontend(pid: int) -> bool:
    if "next-server" not in _pid_command(pid):
        return False
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    cwd = "\n".join(line[1:] for line in result.stdout.splitlines() if line.startswith("n"))
    return str(SUPPORT) in cwd or "web-runtime" in cwd


def _port_listener_pids(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-ti", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(token) for token in result.stdout.split() if token.isdigit()]


def _terminate_pid(pid: int, grace: float) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            return 200 <= int(response.status) < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _instance_is_current(runtime: dict) -> bool:
    return runtime.get("app_version") == APP_VERSION and runtime.get("build_id") == BUILD_ID


def _instance_is_healthy(runtime: dict) -> bool:
    pid = runtime.get("pid", runtime.get("launcher_pid"))
    if not isinstance(pid, int) or not _pid_alive(pid):
        return False
    url = runtime.get("url")
    backend_port = runtime.get("backend_port")
    if not isinstance(url, str) or not isinstance(backend_port, int):
        return False
    if not _probe(url.rstrip("/") + "/chat"):
        return False
    if not _probe(f"http://127.0.0.1:{backend_port}/health/ready"):
        return False
    return True


def _stop_port_listeners(runtime: dict) -> None:
    for key in ("backend_port", "frontend_port"):
        port = runtime.get(key)
        if not isinstance(port, int):
            continue
        for pid in _port_listener_pids(port):
            if _pid_is_old_backend(pid) or _pid_is_old_frontend(pid):
                _terminate_pid(pid, 2.0)


def _stop_instance(runtime: dict) -> None:
    pid = runtime.get("pid", runtime.get("launcher_pid"))
    if isinstance(pid, int) and _pid_alive(pid) and _pid_is_launcher(pid):
        _terminate_pid(pid, 8.0)
    _stop_port_listeners(runtime)


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

    runtime = _read_runtime()
    if runtime:
        if _instance_is_current(runtime) and _instance_is_healthy(runtime):
            print(f"DeepTutor is already running at {runtime.get('url')}", flush=True)
            return 0
        RUNTIME_FILE.unlink(missing_ok=True)
        print(
            "Stopping previous DeepTutor instance "
            f"(version {runtime.get('app_version')}, build {runtime.get('build_id')})",
            flush=True,
        )
        _stop_instance(runtime)

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
            "launcher_pid": os.getpid(),
            "app_version": APP_VERSION,
            "build_id": BUILD_ID,
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
