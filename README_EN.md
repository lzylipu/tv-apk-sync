# 📺 TV APK Sync

> English page. 简体中文见 [README.md](./README.md).

A local web console for Android TV and projectors. It checks one GitHub release line, or one direct APK link, once a day.

![status](https://img.shields.io/badge/status-active-success)
![license](https://img.shields.io/badge/license-MIT-blue)

## 📖 Overview

Save a device, a name, and a link. Cached files can be downloaded, deleted, or pushed. Installed apps can be stopped, cleared, disabled, or removed. Processes are sorted by memory.

## 🚀 Quick Start

```bash
git clone https://github.com/lzylipu/tv-apk-sync.git
cd tv-apk-sync
docker compose up -d
```

Open `http://HOST:8088`. The TV needs network debugging on port `5555`.

## ⚙️ Configuration

| Field | Meaning |
|---|---|
| `apps[].source` | `owner/repo`, a GitHub URL, or a direct `.apk` URL |
| `pull_hours` | How often to look for a new version. Default 24 |
| `install_minutes` | How often to install a downloaded package while the TV is on |
| `GITHUB_TOKEN` | Private repositories only. Set it in the environment |

A GitHub release URL is tracked by filename shape. `1.8.1` changing to `1.8.2` stays on the same line. A non-GitHub URL downloads that one file only.

## 📄 License

[MIT](./LICENSE)
