# 📺 TV APK Sync

> English page. 简体中文见 [README.md](./README.md).

A local web console for Android TV and projectors. It checks one GitHub release line, or one direct APK link, once a day.

![status](https://img.shields.io/badge/status-active-success)
![license](https://img.shields.io/badge/license-MIT-blue)

## 📖 Overview

Save a device, a name, and a link. Cached files can be downloaded, deleted, or pushed. Installed apps can be stopped, cleared, disabled, or removed. Processes are sorted by memory.

## 🚀 Quick Start

The TV needs developer options, USB debugging, and network debugging on port `5555`. The machine running Docker must be on the same LAN.

```bash
mkdir -p /volume1/docker/tv-apk-sync/data
docker pull lzylipu/tv-apk-sync:latest
docker run -d \
  --name tv-apk-sync \
  --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=3 \
  -e TZ=Asia/Shanghai \
  -p 8088:8080 \
  -v /volume1/docker/tv-apk-sync/data:/data \
  lzylipu/tv-apk-sync:latest
```

Change `/volume1/docker/tv-apk-sync/data` to your own directory. Open `http://HOST:8088`.

| Flag | Meaning |
|---|---|
| `-p 8088:8080` | Browser uses 8088. The page inside the container listens on 8080 |
| `-v .../data:/data` | Config, logs, and downloaded APKs. Removing the container does not delete them |
| `-e TZ=Asia/Shanghai` | Log time is Beijing time |
| `-e GITHUB_TOKEN` | Private GitHub repositories only. Leave it out for public ones |

The first visit writes `config.json` in that directory. Add the TV address and the app link in the page. You do not edit the file by hand.

`docker compose up -d` from a clone does the same port and volume mapping: `8088:8080` and `./data:/data`.

Version checks default to every 24 hours. A package already downloaded is installed on the next 5-minute pass while the TV is on.

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
