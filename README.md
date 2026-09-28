# 📺 电视装包 / TV APK Sync

> 🌐 简体中文 | [English](./README_EN.md)

在电脑或 NAS 上开一个网页，连局域网里的电视和投影仪，看已装应用、按内存看进程、把 APK 推上去。GitHub 仓库或直链有新版时，每天查一次，设备开着再装。

A local web console for Android TV and projectors on the same LAN. It lists apps, sorts processes by memory, pushes APKs, and checks a GitHub line or a direct link once a day.

![status](https://img.shields.io/badge/status-active-success)
![license](https://img.shields.io/badge/license-MIT-blue)
![language](https://img.shields.io/badge/language-Python-yellow)

## 📖 简介 / Overview

网页左边是导航，右边按页操作。设备、软件、通知先保存。软件页可以一次导入多个本地 APK。本地包能下载、删除、推送到选中的设备。已装应用可以强停、清缓存、清数据、停用、启用、卸载。进程按内存从大到小排。设备没开时，这两页显示「不在线」。

The left side is navigation. Devices, sources, and notices are saved first. Cached packages stay grouped under the name and link you entered. Installed apps can be stopped, cleared, disabled, or removed. Processes are ordered by memory.

## ✨ 特性 / Features

- 📺 网页看设备是否在线，显示型号、安卓版本、CPU
- 📦 填 GitHub 仓库，或一条 APK 直链。不用先填包名
- 🔁 GitHub 直链会记住文件名骨架。`1.8.1` 后面变成 `1.8.2`、`1.9.1`，仍认同一条发布线
- 🧠 下载后用 `aapt` 读安卓包名和版本，再和电视上的版本比
- ⏰ 查版本默认 24 小时一次。设备开着时，短间隔只负责安装已经下好的包
- 🔔 飞书、PushPlus、Bark 三个填一个。没填就只写页面里的日志
- 📤 通知页可以导出、导入设备和软件、通知、两个间隔。文件就是 `config.yaml`。导入后还要点保存
- 📥 软件页可以拖入多个 APK。文件放在 `apk` 根目录，本地包页可以查看、推送、下载、删除
- 🧾 日志只显示最近 50 条，最新的在最上面
- 🧹 同一软件只留当前这一版。64 位和 32 位都留，旧版删掉
- 🛑 签名不一致不卸载，避免把应用数据清掉

## 🚀 快速开始 / Quick Start

### 前置条件 / Prerequisites

电视或投影仪打开开发者选项、USB 调试、网络调试，端口固定 `5555`。跑容器的设备和电视在同一个局域网。

Android 11 及以上的无线调试端口会变。要固定 5555，先连上后执行一次：

```bash
adb tcpip 5555
```

### 命令行启动 / docker run

不用先克隆仓库。建一个空目录给配置和已下载的包，再拉镜像。

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

群晖以外的机器，把 `/volume1/docker/tv-apk-sync/data` 换成你自己的目录，例如 `/opt/tv-apk-sync/data`。

| 参数 | 含义 | 为什么需要 |
|---|---|---|
| `-p 8088:8080` | 浏览器开 `8088`，容器里网页听 `8080` | 8080 常被别的服务占用，外面固定 8088 |
| `-v .../data:/data` | 配置、记录、已下载的 APK 都在这个目录 | 删容器、换镜像不会把配置和包清掉 |
| `-e TZ=Asia/Shanghai` | 日志用北京时间 | 对得上页面里的记录 |
| `--restart unless-stopped` | 退出后自己起来，手动停了除外 | 不用盯着 |
| `--log-opt max-size=10m --log-opt max-file=3` | 日志最多约 30MB | 长期挂着不会把盘写满 |

私有仓库再加一行 `-e GITHUB_TOKEN=你的token`。公开仓库不用。

浏览器打开 `http://宿主机IP:8088`。第一次进去，「设备」填电视 IP 和 `5555`，「软件」填名称和链接，「通知」填推送和间隔，点保存。配置会写到挂载目录里的 `config.yaml`，不用手改文件。

### 目录里有什么 / What is in data

| 文件 | 作用 |
|---|---|
| `config.yaml` | 设备、软件链接、通知、查版本间隔、设备检测间隔。第一次启动自动生成 |
| `state.json` | 上次查版本的时间、每台设备已装到哪一版 |
| `apk/` | 已经下载或上传的安装包。链接拉下来的按软件分目录，只留当前这一版，64 位和 32 位都留。页面导入的 APK 直接放在这个目录下 |
| `sync.log` | 页面「日志」读的文件，页面只显示最近 50 条 |

`config.example.yaml` 只是仓库里的例子，容器不读它。第一次启动会在挂载目录生成空的 `config.yaml`。

### 用 compose / Compose

```bash
git clone https://github.com/lzylipu/tv-apk-sync.git
cd tv-apk-sync
docker compose up -d
```

compose 里已经写了同样的端口 `8088:8080` 和目录 `./data:/data`。浏览器同样开 `http://宿主机IP:8088`。

### 运行 / Usage

公开仓库不用 token。私有仓库在 `docker run` 或 compose 里加 `GITHUB_TOKEN`。

电视没开时这一轮只记「不在线」，不发失败通知。查到新版会先下到 `data/apk`。设备开着时，按通知页里的设备检测间隔安装已经下好、版本更高的包。查 GitHub 按通知页里的查版本间隔，不跟着设备检测走。

## ⚙️ 配置 / Configuration

网页里能改。文件在挂载目录的 `config.yaml`，不是容器里面一份会丢的文件。

| 字段 | 作用 |
|---|---|
| `devices[].ip` / `port` | 电视地址，端口默认 5555 |
| `apps[].name` | 页面上显示的名称 |
| `apps[].source` | `用户名/仓库`、仓库网址，或以 `.apk` 结尾的直链 |
| `notify.feishu` | 飞书机器人 webhook |
| `notify.pushplus` | PushPlus token |
| `notify.bark` | Bark 地址，形如 `https://api.day.app/你的key` |
| `pull_hours` | 查版本间隔，默认 24 小时 |
| `install_minutes` | 设备检测间隔，通知页配置，状态刷新和安装都用这个值 |
| `GITHUB_TOKEN` | 只有私有仓库需要，环境变量，不写进网页 |

GitHub 发布页直链会按文件名骨架跟踪新版本。不是 GitHub 发布页的直链，只下载你填的那一个文件。

## 🧪 测试 / Testing

```bash
python3 -m unittest tests.abi_select tests.config_cache -v
```

## 📁 项目结构 / Project Structure

```
tv-apk-sync/
├── app.py
├── index.html
├── Dockerfile
├── docker-compose.yml
├── config.example.yaml
├── tests/
│   ├── abi_select.py
│   └── config_cache.py
└── .github/workflows/docker-publish.yml
```

## 🔧 故障排查 / Troubleshooting

| 问题 | 处理 |
|---|---|
| 设备不在线 | 电视没开，或 5555 没开。IP 变了就在路由器里做绑定 |
| 签名不一致 | 不自动卸载。要换签名，先在电视上手动卸一次 |
| 直链没有更新 | 新文件名和原来的骨架不同，或新版本排在仓库最近 30 条发布之外 |
| 私有仓库失败 | compose 里加 `GITHUB_TOKEN` |

## 🤝 贡献 / Contributing

问题和改动直接发到本仓库。

Issues and changes go to this repository.

## 📄 许可证 / License

[MIT](./LICENSE)
