#!/usr/bin/env python3
"""Build self-contained macOS DeepTutor.app bundles and DMG installers.

The script clones the upstream HKUDS/DeepTutor repository at a requested ref,
builds the Next.js standalone frontend and the Python wheel, bundles a portable
CPython and Node.js runtime, and produces an ad-hoc-signed .app plus a DMG.

No Python or Node.js installation is required on the target Mac.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

PACKAGER_ROOT = Path(__file__).resolve().parent
MACOS_DIR = PACKAGER_ROOT / "macos"
BUILD_DIR = PACKAGER_ROOT / "build"
CACHE_DIR = BUILD_DIR / "cache"
SOURCE_DIR = BUILD_DIR / "upstream" / "DeepTutor"
WHEEL_DIR = BUILD_DIR / "wheels"
DIST_DIR = PACKAGER_ROOT / "dist"

UPSTREAM_URL = "https://github.com/HKUDS/DeepTutor.git"
DEFAULT_REF = "v1.6.15"
NODE_VERSION = "22.23.4"
NODE_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/nodejs-release"
PYTHON_RELEASE = "20261026"
PYTHON_VERSION = "3.11.18"
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
PYPI_TRUSTED_HOST = "pypi.tuna.tsinghua.edu.cn"
ARCH_ASSET = {"arm64": "aarch64", "x86_64": "x86_64"}
NODE_ASSET = {"arm64": "arm64", "x86_64": "x64"}


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    log(" ".join(str(part) for part in command))
    subprocess.run(command, cwd=cwd, env=env, check=True)


def download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        log(f"cached {destination.name}")
        return destination
    log(f"downloading {url}")
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "DeepTutor-macOS-builder"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    partial.replace(destination)
    return destination


def ensure_source(ref: str, *, fetch: bool = True) -> Path:
    if not (SOURCE_DIR / ".git").exists():
        SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
        if SOURCE_DIR.exists():
            shutil.rmtree(SOURCE_DIR)
        run(["git", "clone", "--filter=blob:none", UPSTREAM_URL, str(SOURCE_DIR)])

    if fetch:
        result = subprocess.run(
            ["git", "-C", str(SOURCE_DIR), "fetch", "--tags", "--force", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            log("warning: git fetch failed; using the local checkout")
    is_tag = subprocess.run(
        ["git", "-C", str(SOURCE_DIR), "rev-parse", "--verify", f"refs/tags/{ref}"],
        capture_output=True,
        text=True,
        check=False,
    )
    checkout = ref if is_tag.returncode == 0 else f"origin/{ref}"
    run(["git", "-C", str(SOURCE_DIR), "checkout", "--force", checkout])
    commit = subprocess.run(
        ["git", "-C", str(SOURCE_DIR), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    log(f"upstream {ref} at {commit}")
    return SOURCE_DIR


def read_version(source: Path) -> str:
    data = (source / "deeptutor" / "__version__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', data, re.MULTILINE)
    if not match:
        raise SystemExit("Cannot read deeptutor.__version__ from upstream")
    return match.group(1)


def node_version(node: Path) -> tuple[int, ...]:
    result = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=False)
    match = re.match(r"v(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.groups())


def resolve_build_node(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if shutil.which("node"):
        candidates.append(Path(shutil.which("node") or ""))
    for candidate in candidates:
        if candidate.is_file() and node_version(candidate) >= (22, 0, 0):
            log(f"using Node {candidate}")
            return candidate

    host = platform.machine()
    if host not in NODE_ASSET:
        raise SystemExit(f"Unsupported host architecture: {host}")
    filename = f"node-v{NODE_VERSION}-darwin-{NODE_ASSET[host]}.tar.gz"
    archive = download(f"{NODE_MIRROR}/v{NODE_VERSION}/{filename}", CACHE_DIR / filename)
    extract_dir = BUILD_DIR / "tools" / f"node-{NODE_VERSION}-{host}"
    if not extract_dir.exists():
        extract_dir.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            tar.extractall(extract_dir)
    node = extract_dir / f"node-v{NODE_VERSION}-darwin-{NODE_ASSET[host]}" / "bin" / "node"
    if not node.is_file():
        raise SystemExit(f"Missing Node binary in {archive}")
    log(f"using downloaded Node {node}")
    return node


def build_web(source: Path, node_bin: Path) -> Path:
    web = source / "web"
    npm = node_bin.parent / "npm"
    if not npm.is_file():
        raise SystemExit(f"npm not found next to {node_bin}")

    environment = os.environ.copy()
    environment["PATH"] = f"{node_bin.parent}:{environment.get('PATH', '/usr/bin:/bin')}"
    environment["NEXT_PUBLIC_API_BASE"] = "__NEXT_PUBLIC_API_BASE_PLACEHOLDER__"
    environment["NEXT_PUBLIC_AUTH_ENABLED"] = "__NEXT_PUBLIC_AUTH_ENABLED_PLACEHOLDER__"
    environment.pop("DEEPTUTOR_NEXT_DIST_DIR", None)

    if not (web / "node_modules" / "next").exists():
        run([str(npm), "ci", "--legacy-peer-deps"], cwd=web, env=environment)

    log("building Next.js standalone frontend")
    run([str(npm), "run", "build"], cwd=web, env=environment)

    standalone = web / ".next" / "standalone"
    if not (standalone / "server.js").is_file():
        raise SystemExit("Next.js build did not produce web/.next/standalone/server.js")
    return standalone


def prepare_wheel_source(source: Path, standalone: Path) -> Path:
    target = BUILD_DIR / "wheel-src"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        item = source / name
        if item.exists():
            shutil.copy2(item, target / name)
    for name in ("deeptutor", "deeptutor_cli"):
        shutil.copytree(
            source / name,
            target / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )

    package = target / "deeptutor_web"
    package.mkdir(parents=True)
    init_file = source / "deeptutor_web" / "__init__.py"
    if init_file.exists():
        shutil.copy2(init_file, package / "__init__.py")
    else:
        (package / "__init__.py").write_text('"""Packaged web assets."""\n', encoding="utf-8")
    shutil.copytree(standalone, package, dirs_exist_ok=True)

    static = source / "web" / ".next" / "static"
    if static.is_dir():
        shutil.copytree(static, package / ".next" / "static", dirs_exist_ok=True)
    public = source / "web" / "public"
    if public.is_dir():
        shutil.copytree(public, package / "public", dirs_exist_ok=True)
    (package / "BUILD_INFO").write_text(
        "Generated by DeepTutor-macOS from web/.next/standalone.\n",
        encoding="utf-8",
    )
    return target


def build_wheel(source: Path) -> Path:
    if WHEEL_DIR.exists():
        shutil.rmtree(WHEEL_DIR)
    WHEEL_DIR.mkdir(parents=True)
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(WHEEL_DIR),
            str(source),
        ]
    )

    wheels = sorted(WHEEL_DIR.glob("*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise SystemExit("Wheel build produced no .whl")

    wheel = wheels[-1]
    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        if not any(name.endswith("deeptutor_web/server.js") for name in archive.namelist()):
            raise SystemExit(f"{wheel.name} does not contain deeptutor_web/server.js")
    log(f"wheel: {wheel.name} ({wheel.stat().st_size / 1024 / 1024:.1f} MB)")
    return wheel


def _fallback_python_url(target: str) -> str:
    name = (
        f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-"
        f"{ARCH_ASSET[target]}-apple-darwin-install_only.tar.gz"
    )
    from urllib.parse import quote

    return (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PYTHON_RELEASE}/{quote(name)}"
    )


def python_asset_url(target: str) -> str:
    cache = CACHE_DIR / "python-build-standalone.json"
    if not cache.exists():
        try:
            request = urllib.request.Request(
                "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest",
                headers={
                    "User-Agent": "DeepTutor-macOS-builder",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as error:
            log(f"GitHub API unavailable ({error}); using pinned Python release")
            return _fallback_python_url(target)
    else:
        payload = json.loads(cache.read_text(encoding="utf-8"))

    wanted = f"{ARCH_ASSET[target]}-apple-darwin-install_only.tar.gz"
    matches = [
        asset
        for asset in payload.get("assets", [])
        if asset.get("name", "").startswith(f"cpython-{PYTHON_VERSION.split('.')[0]}.{PYTHON_VERSION.split('.')[1]}.")
        and asset.get("name", "").endswith(wanted)
    ]
    if not matches:
        log("No matching release asset; using pinned Python release")
        return _fallback_python_url(target)
    matches.sort(key=lambda asset: asset["name"])
    return matches[-1]["browser_download_url"]


def _arch_wrapper(target: str) -> list[str]:
    host = platform.machine()
    if target == host:
        return []
    if host != "arm64":
        raise SystemExit(f"Cannot build {target} on {host}; use an Apple Silicon host")
    return ["arch", "-x86_64"]


def prepare_python(target: str, wheel: Path, *, force: bool) -> Path:
    runtime = BUILD_DIR / target / "python"
    marker = runtime / ".deeptutor-wheel"

    if not runtime.exists():
        archive = download(python_asset_url(target), CACHE_DIR / f"python-{target}.tar.gz")
        log(f"extracting portable Python for {target}")
        target_dir = BUILD_DIR / target
        target_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            tar.extractall(target_dir)
        if not runtime.exists():
            raise SystemExit(f"Portable Python archive did not create {runtime}")

    python = runtime / "bin" / "python3"
    if not python.exists():
        raise SystemExit(f"Missing portable Python executable: {python}")

    installed = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    if force or installed != wheel.name:
        command = _arch_wrapper(target) + [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--no-warn-script-location",
            "--only-binary=:all:",
            "--force-reinstall",
            str(wheel),
        ]
        environment = os.environ.copy()
        environment["PIP_INDEX_URL"] = PYPI_MIRROR
        environment["PIP_TRUSTED_HOST"] = PYPI_TRUSTED_HOST
        environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        environment["PIP_NO_INPUT"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        run(command, env=environment)
        marker.write_text(wheel.name + "\n", encoding="utf-8")
    else:
        log(f"reusing installed wheel {wheel.name}")

    check_code = (
        "from deeptutor.__version__ import __version__; "
        "import deeptutor_web, uvicorn; "
        "print(__version__)"
    )
    command = _arch_wrapper(target) + [str(python), "-c", check_code]
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    result = subprocess.run(
        command,
        cwd=BUILD_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Bundled Python cannot import DeepTutor:\n{result.stdout}\n{result.stderr}"
        )
    log(f"bundled Python imports OK: {result.stdout.strip()}")

    for folder in ("test", "idlelib", "turtledemo", "ensurepip"):
        candidate = runtime / "lib" / "python3.11" / folder
        if candidate.is_dir():
            shutil.rmtree(candidate)
    for folder in ("tk9.0", "tcl9", "thread3.0.6", "itcl4.3.8", "tcl8", "tk8.6"):
        candidate = runtime / "lib" / folder
        if candidate.is_dir():
            shutil.rmtree(candidate)
    tkinter = runtime / "lib" / "python3.11" / "tkinter"
    if tkinter.is_dir():
        shutil.rmtree(tkinter)
    for pattern in ("libtcl*.dylib", "libtk*.dylib", "_tkinter*.so"):
        for candidate in list((runtime / "lib").rglob(pattern)):
            candidate.unlink(missing_ok=True)
    for cache in (runtime / "lib").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return runtime


def prepare_node(target: str) -> Path:
    filename = f"node-v{NODE_VERSION}-darwin-{NODE_ASSET[target]}.tar.gz"
    archive = download(f"{NODE_MIRROR}/v{NODE_VERSION}/{filename}", CACHE_DIR / filename)
    extract_dir = CACHE_DIR / f"node-{NODE_VERSION}-{target}"
    if not extract_dir.exists():
        extract_dir.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            tar.extractall(extract_dir)
    node = extract_dir / f"node-v{NODE_VERSION}-darwin-{NODE_ASSET[target]}" / "bin" / "node"
    if not node.is_file():
        raise SystemExit(f"Missing Node binary in {archive}")
    return node


def build_icon(source: Path) -> Path:
    try:
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise SystemExit("Pillow is required. Run: pip install -r requirements.txt") from error

    iconset = BUILD_DIR / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=190, fill=(245, 245, 244, 255))

    logo = Image.open(source / "assets" / "figs" / "logo" / "logo.png").convert("RGBA")
    logo.thumbnail((720, 720), Image.LANCZOS)
    image.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))

    for name, pixels in {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }.items():
        image.resize((pixels, pixels), Image.LANCZOS).save(iconset / name)

    output = BUILD_DIR / "AppIcon.icns"
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(output)])
    return output


def swiftc() -> str:
    result = subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True, text=True)
    path = result.stdout.strip() if result.returncode == 0 else ""
    if not path:
        path = shutil.which("swiftc") or ""
    if not path:
        raise SystemExit("swiftc not found. Install Xcode Command Line Tools.")
    return path


def macos_sdk() -> str:
    result = subprocess.run(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def compile_launcher(target: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    platform_target = "arm64-apple-macosx13.0" if target == "arm64" else "x86_64-apple-macosx13.0"
    run(
        [
            swiftc(),
            "-O",
            "-swift-version",
            "5",
            "-sdk",
            macos_sdk(),
            "-target",
            platform_target,
            "-framework",
            "Cocoa",
            "-framework",
            "WebKit",
            str(MACOS_DIR / "main.swift"),
            "-o",
            str(destination),
        ]
    )
    destination.chmod(0o755)


def clone_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["cp", "-Rc", str(source), str(destination)], check=False)
    if result.returncode != 0:
        shutil.copytree(source, destination, symlinks=True)


def assemble_app(target: str, python_runtime: Path, node_bin: Path, icon: Path, app_version: str) -> Path:
    app = BUILD_DIR / target / "DeepTutor.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    resources = contents / "Resources"
    macos = contents / "MacOS"
    resources.mkdir(parents=True)
    macos.mkdir(parents=True)

    clone_tree(python_runtime, resources / "python")
    (resources / "node" / "bin").mkdir(parents=True)
    shutil.copy2(node_bin, resources / "node" / "bin" / "node")
    (resources / "node" / "bin" / "node").chmod(0o755)
    shutil.copy2(MACOS_DIR / "bootstrap.py", resources / "bootstrap.py")
    shutil.copy2(icon, resources / "AppIcon.icns")

    plist = (MACOS_DIR / "Info.plist").read_text(encoding="utf-8").replace("__VERSION__", app_version)
    (contents / "Info.plist").write_text(plist, encoding="utf-8")

    compile_launcher(target, macos / "DeepTutor")

    run(["codesign", "--force", "--sign", "-", str(app)])
    run(["codesign", "--verify", "--strict", str(app)])
    run(["plutil", "-lint", str(contents / "Info.plist")])
    return app


def make_dmg(target: str, app: Path, app_version: str, output_dir: Path) -> Path:
    staging = BUILD_DIR / target / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    clone_tree(app, staging / "DeepTutor.app")
    os.symlink("/Applications", staging / "Applications")

    output = output_dir / f"DeepTutor-{app_version}-{target}.dmg"
    output_dir.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    run(
        [
            "hdiutil",
            "create",
            "-volname",
            "DeepTutor",
            "-srcfolder",
            str(staging),
            "-ov",
            "-format",
            "UDZO",
            str(output),
        ]
    )
    run(["hdiutil", "verify", str(output)])
    shutil.rmtree(staging)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=DEFAULT_REF, help=f"Upstream git tag or branch (default: {DEFAULT_REF})")
    parser.add_argument("--arch", action="append", choices=["arm64", "x86_64"], help="Target architecture")
    parser.add_argument("--node-bin", help="Node.js 22+ binary used for the frontend build")
    parser.add_argument("--skip-web", action="store_true", help="Reuse the upstream web/.next/standalone build")
    parser.add_argument("--skip-wheel", action="store_true", help="Reuse the newest wheel in build/wheels")
    parser.add_argument("--no-fetch", action="store_true", help="Use the existing upstream checkout without fetching")
    parser.add_argument("--force-reinstall", action="store_true", help="Reinstall the wheel into the portable Python")
    parser.add_argument("--output-dir", default=str(DIST_DIR), help="DMG output directory")
    parser.add_argument("--clean", action="store_true", help="Remove the build directory before starting")
    args = parser.parse_args()

    if args.clean and BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)

    source = ensure_source(args.ref, fetch=not args.no_fetch)
    app_version = read_version(source)
    targets = args.arch or [platform.machine()]
    log(f"DeepTutor {app_version}; targets: {', '.join(targets)}")

    standalone = None
    if args.skip_web:
        standalone = source / "web" / ".next" / "standalone"
        if not args.skip_wheel and not (standalone / "server.js").is_file():
            raise SystemExit("--skip-web requires web/.next/standalone/server.js")
    else:
        standalone = build_web(source, resolve_build_node(args.node_bin))

    if args.skip_wheel:
        wheels = sorted(WHEEL_DIR.glob("*.whl"), key=lambda path: path.stat().st_mtime)
        if not wheels:
            raise SystemExit("--skip-wheel requires an existing wheel in build/wheels")
        wheel = wheels[-1]
    else:
        wheel = build_wheel(prepare_wheel_source(source, standalone))

    icon = build_icon(source)
    output_dir = Path(args.output_dir).expanduser().resolve()
    packages: list[tuple[str, Path]] = []
    for target in targets:
        python_runtime = prepare_python(target, wheel, force=args.force_reinstall)
        node = prepare_node(target)
        app = assemble_app(target, python_runtime, node, icon, app_version)
        dmg = make_dmg(target, app, app_version, output_dir)
        packages.append((target, dmg))

    log("done")
    for target, dmg in packages:
        print(f"  {target:8s} {dmg}  ({dmg.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
