# DeepTutor macOS Packager

[English](README.md) | [简体中文](README.zh-CN.md)

Build a self-contained `DeepTutor.app` and `.dmg` from the upstream
[HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) repository.

The result needs no Python or Node.js on the target Mac. It bundles a portable
CPython 3.11 runtime, Node.js 22, the packaged Next.js standalone server, and a
native Swift `WKWebView` window with a menu bar.

## Download

- [Apple Silicon DMG](https://github.com/yuyaxiong/DeepTutor-macOS/releases/download/v1.6.15-r4/DeepTutor-1.6.15-arm64.dmg)
- [Intel DMG](https://github.com/yuyaxiong/DeepTutor-macOS/releases/download/v1.6.15-r4/DeepTutor-1.6.15-x86_64.dmg)

`r2` fixed Cmd+C/Cmd+V in API key fields. `r3` added version/build validation and stale instance cleanup. `r4` adds health monitoring with automatic restart and a 15s graceful shutdown.

Or build locally from source with the steps below.

## What it produces

- `DeepTutor-<version>-arm64.dmg` for Apple Silicon
- `DeepTutor-<version>-x86_64.dmg` for Intel Macs

Each DMG contains `DeepTutor.app` and an `Applications` shortcut.

## Requirements

- macOS 13 or later
- Xcode Command Line Tools (`swiftc`, `iconutil`, `hdiutil`, `codesign`)
- Python 3.11+
- Apple Silicon for both targets, or an Intel Mac for `x86_64`
- Network access to GitHub, PyPI, and the configured mirrors
- Rosetta 2 when building `x86_64` on Apple Silicon

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python build.py --ref v1.6.15 --arch arm64
python build.py --ref v1.6.15 --arch x86_64
```

Outputs are written to `dist/`.

## Options

| Option | Description |
| --- | --- |
| `--ref` | Upstream tag or branch. Default `v1.6.15`. |
| `--arch` | `arm64` or `x86_64`. Repeat for both. |
| `--node-bin` | Use an existing Node.js 22+ binary for the frontend build. |
| `--skip-web` | Reuse `web/.next/standalone` from the upstream clone. |
| `--skip-wheel` | Reuse the newest wheel under `build/wheels`. |
| `--no-fetch` | Skip `git fetch` and use the existing upstream checkout. |
| `--force-reinstall` | Reinstall the wheel into the portable Python. |
| `--output-dir` | DMG output directory. Default `dist/`. |
| `--clean` | Remove `build/` before starting. |

## Mirrors

The defaults use Tsinghua mirrors for Node.js and PyPI. Override them with:

```bash
export DEEPTUTOR_NODE_MIRROR="https://nodejs.org/dist"
export DEEPTUTOR_PYPI_INDEX="https://pypi.org/simple"
export DEEPTUTOR_PYPI_HOST="pypi.org"
```

## How it works

1. Clone or update `HKUDS/DeepTutor` at the requested ref under `build/upstream/DeepTutor`.
2. Build the Next.js standalone frontend with runtime API-base placeholders.
3. Pack the standalone frontend into the `deeptutor_web` package and build the Python wheel.
4. Download portable CPython for the target architecture.
5. Install the wheel into the portable Python with binary-only dependencies.
6. Download Node.js 22 for the target architecture.
7. Assemble `DeepTutor.app`, compile the Swift launcher, and ad-hoc sign the app.
8. Create the DMG with `hdiutil`.

The app starts the backend on `127.0.0.1:4782` and the frontend on
`127.0.0.1:4783`. If either port is occupied, the bootstrap selects a free pair
and writes it to the runtime settings.

## Install

1. Open the DMG for your Mac.
2. Drag `DeepTutor.app` to `Applications`.
3. Right-click the app and choose **Open** on first launch.

The app is ad-hoc signed, not notarized. If macOS blocks it, run:

```bash
xattr -dr com.apple.quarantine /Applications/DeepTutor.app
```

## First launch

The app starts automatically and opens its main window. Configure the model
provider before chatting:

1. **Settings → Providers** — add provider, base URL, and API key.
2. **Settings → Catalog** — select a model.

Configuration is stored in `~/Library/Application Support/DeepTutor`.
Logs are in `~/Library/Logs/DeepTutor/launcher.log`.

## Distribution notes

This repository builds unsigned apps for local use. Public distribution
requires:

- An Apple Developer ID Application certificate
- Signing of the app and its nested runtime binaries
- Notarization and stapling

## Attribution

DeepTutor is developed by the HKUDS Data Intelligence Lab and licensed under
Apache-2.0. This repository contains only the macOS packaging layer and
downloads the upstream source at build time.
