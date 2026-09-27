# 📺 电视装包 / TV APK Sync

> 🌐 简体中文 | [English](./README_EN.md)

在电脑或 NAS 上开一个网页，连局域网里的电视和投影仪，看已装应用、按内存看进程、把 APK 推上去。GitHub 仓库或直链有新版时，每天查一次，设备开着再装。

A local web console for Android TV and projectors on the same LAN. It lists apps, sorts processes by memory, pushes APKs, and checks a GitHub line or a direct link once a day.

![status](https://img.shields.io/badge/status-active-success)
![license](https://img.shields.io/badge/license-MIT-blue)
![language](https://img.shields.io/badge/language-Python-yellow)

## 📖 简介 / Overview

网页左边是导航，右边按页操作。设备、软件、通知先保存。本地包按你填的名称和链接分层，能下载、删除、推送到选中的设备。已装应用可以强停、清缓存、清数据、停用、启用、卸载。进程按内存从大到小排。

The left side is navigation. Devices, sources, and notices are saved first. Cached packages stay grouped under the name and link you entered. Installed apps can be stopped, cleared, disabled, or removed. Processes are ordered by memory.

## ✨ 特性 / Features

- 📺 网页看设备是否在线，显示型号、安卓版本、CPU
- 📦 填 GitHub 仓库，或一条 APK 直链。不用先填包名
- 🔁 GitHub 直链会记住文件名骨架。`1.8.1` 后面变成 `1.8.2`、`1.9.1`，仍认同一条发布线
- 🧠 下载后用 `aapt` 读安卓包名和版本，再和电视上的版本比
- ⏰ 查版本默认 24 小时一次。设备开着时，短间隔只负责安装已经下好的包
- 🔔 飞书、PushPlus、Bark 三个填一个。没填就只写页面里的记录
- 🛑 签名不一致不卸载，避免把应用数据清掉

## 🚀 快速开始 / Quick Start

### 前置条件 / Prerequisites

电视或投影仪打开开发者选项、USB 调试、网络调试，端口固定 `5555`。跑容器的设备和电视在同一个局域网。

Android 11 及以上的无线调试端口会变。要固定 5555，先连上后执行一次：

```bash
adb tcpip 5555
```

### 安装 / Installation

```bash
git clone https://github.com/lzylipu/tv-apk-sync.git
cd tv-apk-sync
docker compose up -d
```

浏览器打开 `http://宿主机IP:8088`。先在「设备」里填电视 IP，再在「软件」里填名称和链接，点保存。

### 运行 / Usage

公开仓库不用 token。私有仓库在 compose 里加 `GITHUB_TOKEN`。

| 参数 | 含义 | 为什么需要 |
|---|---|---|
| `restart: unless-stopped` | 退出后自己起来 | 不用盯着 |
| `TZ=Asia/Shanghai` | 日志用北京时间 | 对得上记录 |
| 日志 10MB × 3 | 日志不会把盘写满 | 长期挂着 |
| `./data:/data` | 配置、记录、已下的 APK | 重建容器不用重下 |

## ⚙️ 配置 / Configuration

网页里能改。文件在 `data/config.json`。

| 字段 | 作用 |
|---|---|
| `devices[].ip` / `port` | 电视地址，端口默认 5555 |
| `apps[].name` | 页面上显示的名称 |
| `apps[].source` | `用户名/仓库`、仓库网址，或以 `.apk` 结尾的直链 |
| `notify.feishu` | 飞书机器人 webhook |
| `notify.pushplus` | PushPlus token |
| `notify.bark` | Bark 地址，形如 `https://api.day.app/你的key` |
| `pull_hours` | 查版本间隔，默认 24 小时 |
| `install_minutes` | 设备在线时尝试安装的间隔，默认 5 分钟 |
| `GITHUB_TOKEN` | 只有私有仓库需要，环境变量，不写进网页 |

GitHub 发布页直链会按文件名骨架跟踪新版本。不是 GitHub 发布页的直链，只下载你填的那一个文件。

## 🧪 测试 / Testing

```bash
python3 -m unittest discover -s tests -v
```

## 📁 项目结构 / Project Structure

```
tv-apk-sync/
├── app.py
├── index.html
├── Dockerfile
├── docker-compose.yml
├── config.example.json
├── tests/
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
