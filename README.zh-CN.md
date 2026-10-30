# DeepTutor macOS 打包工具

[English](README.md) | [简体中文](README.zh-CN.md)

从上游 [HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) 构建自包含的 `DeepTutor.app` 和 `.dmg`。

产物不需要目标 Mac 安装 Python 或 Node.js。应用自带便携 CPython 3.11、Node.js 22、Next.js 独立服务，以及原生 Swift `WKWebView` 窗口和菜单栏。

## 直接下载

- [Apple Silicon 安装包](https://github.com/yuyaxiong/DeepTutor-macOS/releases/download/v1.6.15-r3/DeepTutor-1.6.15-arm64.dmg)
- [Intel 安装包](https://github.com/yuyaxiong/DeepTutor-macOS/releases/download/v1.6.15-r3/DeepTutor-1.6.15-x86_64.dmg)

`r2` 修复了 API Key 输入框的 Cmd+C/Cmd+V。`r3` 增加版本/构建号校验、前后端健康检查和旧实例清理。

需要自行构建时，按下面的步骤操作。

## 构建产物

- `DeepTutor-<版本>-arm64.dmg`：Apple Silicon
- `DeepTutor-<版本>-x86_64.dmg`：Intel Mac

每个 DMG 包含 `DeepTutor.app` 和 `Applications` 快捷方式。

## 环境要求

- macOS 13 或更高
- Xcode Command Line Tools（`swiftc`、`iconutil`、`hdiutil`、`codesign`）
- Python 3.11+
- Apple Silicon 可构建两个架构；Intel Mac 只能构建 `x86_64`
- 网络访问 GitHub、PyPI 和配置的镜像
- 在 Apple Silicon 上构建 `x86_64` 需要 Rosetta 2

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python build.py --ref v1.6.15 --arch arm64
python build.py --ref v1.6.15 --arch x86_64
```

产物写入 `dist/`。

## 参数

| 参数 | 说明 |
| --- | --- |
| `--ref` | 上游 tag 或分支。默认 `v1.6.15`。 |
| `--arch` | `arm64` 或 `x86_64`。可重复传入。 |
| `--node-bin` | 使用已有的 Node.js 22+ 二进制构建前端。 |
| `--skip-web` | 复用上游 `web/.next/standalone`。 |
| `--skip-wheel` | 复用 `build/wheels` 中最新的 wheel。 |
| `--no-fetch` | 跳过 `git fetch`，使用已有上游检出。 |
| `--force-reinstall` | 重新安装 wheel 到便携 Python。 |
| `--output-dir` | DMG 输出目录。默认 `dist/`。 |
| `--clean` | 开始前删除 `build/`。 |

## 镜像

默认使用清华镜像下载 Node.js 和 PyPI 依赖。可以覆盖：

```bash
export DEEPTUTOR_NODE_MIRROR="https://nodejs.org/dist"
export DEEPTUTOR_PYPI_INDEX="https://pypi.org/simple"
export DEEPTUTOR_PYPI_HOST="pypi.org"
```

## 工作原理

1. 在 `build/upstream/DeepTutor` 克隆或更新指定 ref 的上游代码。
2. 使用运行时 API 占位符构建 Next.js 独立前端。
3. 把独立前端打包进 `deeptutor_web`，并构建 Python wheel。
4. 下载目标架构的便携 CPython。
5. 用仅二进制依赖把 wheel 安装进便携 Python。
6. 下载目标架构的 Node.js 22。
7. 组装 `DeepTutor.app`，编译 Swift 启动器，并对应用做 ad-hoc 签名。
8. 用 `hdiutil` 生成 DMG。

应用会在 `127.0.0.1:4782` 启动后端，在 `127.0.0.1:4783` 启动前端。端口被占用时，启动引导会选择空闲端口并写入运行时配置。

## 安装

1. 打开对应芯片的 DMG。
2. 把 `DeepTutor.app` 拖到 `Applications`。
3. 首次启动时右键应用，选择“打开”。

应用是 ad-hoc 签名，未公证。如果 macOS 阻止打开，执行：

```bash
xattr -dr com.apple.quarantine /Applications/DeepTutor.app
```

## 首次启动

应用会自动启动并打开主窗口。聊天前先配置模型：

1. **Settings → Providers**：填写 provider、Base URL 和 API Key。
2. **Settings → Catalog**：选择模型。

配置保存在 `~/Library/Application Support/DeepTutor`。
日志保存在 `~/Library/Logs/DeepTutor/launcher.log`。

## 分发说明

本仓库构建的是本地自用未签名应用。公开分发需要：

- Apple Developer ID Application 证书
- 对应用及其嵌套运行时二进制签名
- 公证和 stapling

## 归属

DeepTutor 由 HKUDS Data Intelligence Lab 开发，采用 Apache-2.0 许可证。本仓库只包含 macOS 打包层，并在构建时下载上游源码。
