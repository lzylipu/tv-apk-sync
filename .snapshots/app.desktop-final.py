#!/usr/bin/env python3
"""Watch GitHub releases and install the matching APK onto Android TVs."""

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG = DATA / "config.yaml"
STATE = DATA / "state.json"
CACHE = DATA / "apk"
LOG = DATA / "sync.log"
PAGE_FILE = Path(__file__).with_name("index.html")
UA = "tv-apk-sync"
PULL = {"running": False, "result": ""}
LOCK = threading.Lock()
DEFAULT = {
    "devices": [],
    "apps": [],
    "notify": {"feishu": "", "pushplus": "", "bark": ""},
    "pull_hours": 24,
    "install_minutes": 10,
}


def log(line):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}"
    print(text, flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def load_json(path, default):
    if not path.exists():
        return json.loads(json.dumps(default))
    return json.loads(path.read_text(encoding="utf-8"))


def yaml_quote(value):
    text = str(value)
    if text == "" or any(ch in text for ch in ":#{}[]&*!|>%@`'\"\n\t") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def dump_yaml(data):
    lines = []
    for key in ("devices", "apps", "notify", "pull_hours", "install_minutes"):
        value = data.get(key)
        if key in ("devices", "apps"):
            lines.append(f"{key}:")
            if not value:
                lines.append("  []")
                continue
            fields = ("name", "ip", "port") if key == "devices" else ("name", "source", "min_sdk")
            for item in value:
                first = True
                for field in fields:
                    val = item.get(field, "")
                    if str(val) in ("", "0", "None"):
                        continue
                    mark = "- " if first else "  "
                    first = False
                    lines.append(f"  {mark}{field}: {yaml_quote(val)}")
        elif key == "notify":
            lines.append("notify:")
            notify = value or {}
            for field in ("feishu", "pushplus", "bark"):
                lines.append(f"  {field}: {yaml_quote(notify.get(field, ''))}")
        else:
            lines.append(f"{key}: {int(value or 0)}")
    return "\n".join(lines) + "\n"


def parse_yaml(text):
    data = {"devices": [], "apps": [], "notify": {}}
    section = ""
    current = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            key, _, value = raw.partition(":")
            section = key.strip()
            current = None
            if section in ("pull_hours", "install_minutes"):
                data[section] = int(value.strip() or 0)
            continue
        body = raw.strip()
        if body == "[]":
            continue
        item = body[2:] if body.startswith("- ") else body
        key, _, value = item.partition(":")
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = json.loads(value) if value.startswith('"') else value[1:-1]
        if section in ("devices", "apps") and body.startswith("- "):
            current = {}
            data[section].append(current)
        target = current if section in ("devices", "apps") else data.setdefault(section, {})
        if target is not None and key.strip():
            if key.strip() in ("port", "min_sdk") and str(value).isdigit():
                target[key.strip()] = int(value)
            else:
                target[key.strip()] = value
    return data


def current_config():
    path = CONFIG if CONFIG.exists() else DATA / "config.json"
    if path.suffix == ".json":
        data = load_json(path, DEFAULT)
    elif path.exists():
        data = parse_yaml(path.read_text(encoding="utf-8"))
    else:
        data = json.loads(json.dumps(DEFAULT))
    for key, value in DEFAULT.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    data.pop("github_token", None)
    return data


def save_config(data):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(dump_yaml(data), encoding="utf-8")
    tmp.replace(CONFIG)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mktemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)))
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def http_json(url):
    # 第三方 GitHub 加速代理前缀（免 token，突破 api.github.com 未认证 60 次/小时限流）
    proxies = [
        "https://gh-proxy.com/",
        "https://ghproxy.net/",
        "https://ghfast.top/",
    ]
    attempts = [url]
    if "api.github.com" in url:
        attempts += [p + url for p in proxies]
    last = None
    for target in attempts:
        try:
            req = urllib.request.Request(target, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            continue
    raise last if last else RuntimeError("全部代理都失败")


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%")))
    # 第三方 GitHub 加速代理前缀（免 token，下载 GitHub release/raw 失败时自动走代理）
    proxies = [
        "https://gh-proxy.com/",
        "https://ghproxy.net/",
        "https://ghfast.top/",
    ]
    attempts = [url]
    if "github.com" in url or "githubusercontent.com" in url:
        attempts += [p + url for p in proxies]
    last = None
    for target in attempts:
        try:
            req = urllib.request.Request(target, headers={"User-Agent": UA, "Accept": "application/octet-stream"})
            tmp = dest.with_name(dest.name + ".part")
            try:
                with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as handle:
                    shutil.copyfileobj(resp, handle)
                tmp.replace(dest)
                return
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        except Exception as exc:
            last = exc
            continue
    raise last if last else RuntimeError("全部代理都失败")


def adb(*args, timeout=60):
    try:
        proc = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, f"adb timeout after {timeout}s"


def connect(address):
    adb("connect", address, timeout=20)
    code, out = adb("devices", timeout=15)
    online = any(line.startswith(address) and "\tdevice" in line for line in out.splitlines())
    if online:
        return True, out
    return False, "没开机或 ADB 无线调试(5555)没开"


def prop(address, name):
    code, out = adb("-s", address, "shell", "getprop", name, timeout=20)
    return out.strip() if code == 0 else ""


def soc_vendor(soc):
    soc = (soc or "").strip()
    if not soc:
        return ""
    low = soc.lower()
    if low.startswith(("mt", "mtk", "m7")):   # m7642 也是 MTK 平台代号
        return "MediaTek " + soc.upper()
    if low.startswith(("sdm", "sm8", "sm7", "sm6", "qcom", "msm")):
        return "Qualcomm " + soc.upper()
    if low.startswith(("hi3", "hi7", "hi9", "kirin", "hisicon")):
        return "HiSilicon " + soc.upper()
    if low.startswith(("s9", "s8", "axg", "aml")):
        return "Amlogic " + soc.upper()
    if low.startswith("rk"):
        return "Rockchip " + soc.upper()
    if "exynos" in low:
        return "Samsung Exynos"
    if low.startswith("aml"):
        return "Amlogic " + soc.upper()
    return soc.upper() if len(low) <= 8 else soc


def abi_family(abi):
    abi = (abi or "").lower()
    if "arm64" in abi or "aarch64" in abi:
        return "arm64"
    if "armeabi" in abi or abi.startswith("arm"):
        return "arm"
    if "x86_64" in abi:
        return "x86_64"
    if "x86" in abi:
        return "x86"
    return ""


ARM_PARTS = {
    "0xd03": "Cortex-A53", "0xd05": "Cortex-A55", "0xd07": "Cortex-A57", "0xd08": "Cortex-A72",
    "0xd09": "Cortex-A73", "0xd0a": "Cortex-A75", "0xd0b": "Cortex-A76", "0xd0d": "Cortex-A77",
    "0xd41": "Cortex-A78", "0xd44": "Cortex-X1", "0xd46": "Cortex-A510", "0xd47": "Cortex-A710",
    "0xd48": "Cortex-X2", "0xd4d": "Cortex-A715", "0xd4e": "Cortex-X3", "0xd81": "Cortex-A720",
    "0xd82": "Cortex-X4", "0xc07": "Cortex-A7", "0xc0f": "Cortex-A15",
}


def cpu_cores(address):
    code, out = adb("-s", address, "shell", "grep -m2 'CPU part' /proc/cpuinfo", timeout=15)
    parts = re.findall(r"CPU part\s*:\s*(0x[0-9a-fA-F]+)", out or "")
    names = []
    for part in parts:
        name = ARM_PARTS.get(part.lower())
        if name and name not in names:
            names.append(name)
    return " + ".join(names)


def sdk_int(value):
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else 0


def version_ge(a, b):
    """a >= b 按数字分段比较版本号；非纯数字时退化为字符串相等比较。"""
    if not a or not b:
        return a == b
    pa = [int(x) for x in re.findall(r"\d+", a)][:4]
    pb = [int(x) for x in re.findall(r"\d+", b)][:4]
    if not pa or not pb:
        return a == b
    for x, y in zip(pa, pb):
        if x != y:
            return x > y
    return len(pa) >= len(pb)


def version_gt(a, b):
    """a > b 按数字分段比较版本号；非纯数字时退化为字符串比较。"""
    if not a or not b:
        return bool(a) and not bool(b)
    pa = [int(x) for x in re.findall(r"\d+", a)][:4]
    pb = [int(x) for x in re.findall(r"\d+", b)][:4]
    if not pa or not pb:
        return a > b
    for x, y in zip(pa, pb):
        if x != y:
            return x > y
    return len(pa) > len(pb)


def asset_score(name, family):
    low = name.lower()
    if not low.endswith(".apk"):
        return None
    if family == "arm64" and re.search(r"armeabi-v7a|armv7|x86", low) and "arm64" not in low and "universal" not in low:
        return None
    if family == "arm" and re.search(r"arm64|aarch64|x86", low):
        return None
    if family == "x86_64" and re.search(r"arm|x86(?!_64)", low) and "x86_64" not in low and "universal" not in low:
        return None
    if family == "x86" and re.search(r"arm|x86_64", low):
        return None
    score = 0
    if family == "arm64" and re.search(r"arm64|aarch64|v8a", low):
        score += 50
    if family == "arm" and re.search(r"armeabi-v7a|armv7|v7a", low):
        score += 50
    if family == "x86_64" and "x86_64" in low:
        score += 50
    if family == "x86" and re.search(r"(^|[^_])x86([^_]|$)", low):
        score += 50
    if re.search(r"universal|noarch", low):
        score += 20
    if re.search(r"tv|leanback", low):
        score += 5
    return score


def pick_asset(assets, family):
    ranked = []
    for asset in assets:
        score = asset_score(asset.get("name", ""), family)
        if score is not None:
            ranked.append((score, asset))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def arm_assets(assets):
    seen = []
    for family in ("arm64", "arm"):
        picked = pick_asset(assets, family)
        if picked and picked not in seen:
            seen.append(picked)
    return seen


def asset_for_device(assets, family):
    if family in ("arm64", "arm", ""):
        return pick_asset(assets, family or "arm64") or pick_asset(assets, "arm")
    return pick_asset(assets, family)


def cache_key(source):
    repo = github_repo(source)
    if repo:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", repo)[:80]
    name = urllib.parse.unquote(source.strip().split("?", 1)[0].rstrip("/").split("/")[-1])
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    return (name or "apk")[:80]


def cache_dest(source, tag, name):
    if github_repo(source):
        return CACHE / cache_key(source) / f"{tag}-{name}"
    return CACHE / cache_key(source) / name


def notify(cfg, text):
    url = (cfg.get("notify") or {}).get("url", "").strip()
    if not url:
        return
    body = json.dumps({"title": "电视装包", "body": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as exc:
        log(f"通知失败 {exc}")


def device_stats(address):
    stats = {}
    code, mem = adb("-s", address, "shell", "cat", "/proc/meminfo", timeout=15)
    if code == 0:
        found = dict(re.findall(r"(\w+):\s+(\d+)", mem))
        total = int(found.get("MemTotal") or 0)
        avail = int(found.get("MemAvailable") or found.get("MemFree") or 0)
        if total:
            stats["mem_total"] = total
            stats["mem_used"] = max(0, total - avail)
    code, df = adb("-s", address, "shell", "df", "-k", "/data", timeout=15)
    if code == 0:
        for line in df.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4 and parts[1].isdigit():
                stats["disk_total"] = int(parts[1])
                stats["disk_used"] = int(parts[2])
                stats["disk_free"] = int(parts[3])
                break
    code, up = adb("-s", address, "shell", "cat", "/proc/uptime", timeout=10)
    if code == 0 and up.split():
        stats["uptime"] = int(float(up.split()[0]))
    size = prop(address, "persist.sys.display.size") or prop(address, "sys.display-size")
    if not size:
        code, wm = adb("-s", address, "shell", "wm", "size", timeout=10)
        match = re.search(r"(\d+x\d+)", wm or "")
        size = match.group(1) if match else ""
    if size:
        stats["resolution"] = size.strip()
    stats["brand"] = prop(address, "ro.product.brand")
    stats["serial"] = prop(address, "ro.serialno") or prop(address, "ro.boot.serialno")
    stats["build"] = prop(address, "ro.build.display.id")
    stats["patch"] = prop(address, "ro.build.version.security_patch")
    return {key: value for key, value in stats.items() if value not in ("", None)}


def device_profile(address):
    ok, detail = connect(address)
    if not ok:
        return None, detail or "连不上"
    soc = soc_vendor(prop(address, "ro.board.platform") or prop(address, "ro.hardware"))
    abi = prop(address, "ro.product.cpu.abi")
    abilist = prop(address, "ro.product.cpu.abilist")
    sdk = sdk_int(prop(address, "ro.build.version.sdk"))
    release = prop(address, "ro.build.version.release")
    model = prop(address, "ro.product.model")
    family = abi_family(abi)
    if not family:
        return None, f"认不出 CPU：{abi or abilist or '空'}"
    return {"abi": abi, "abilist": abilist, "sdk": sdk, "release": release, "model": model, "family": family, "soc": soc, "cores": cpu_cores(address)}, ""


def installed_version(address, package):
    code, out = adb("-s", address, "shell", "dumpsys", "package", package, timeout=30)
    if code != 0 or "Unable to find package" in out:
        return ""
    match = re.search(r"versionName=(\S+)", out)
    return match.group(1) if match else ""


ACTION_ERRORS = [
    ("device '", "设备没连上，请确认开机且 ADB 无线调试(5555)已开"),
    ("not found", "设备没连上，请确认开机且 ADB 无线调试(5555)已开"),
    ("not installed for", "这台设备没装这个应用（或已被卸载）"),
    ("Failure [not installed", "这台设备没装这个应用（或已被卸载）"),
    ("DELETE_FAILED_DEVICE_POLICY_MANAGER", "被设备管理器锁定，不能卸载。先在系统设置里取消设备管理器"),
    ("DELETE_FAILED_INTERNAL_ERROR", "系统内部错误，卸载没执行"),
    ("DELETE_FAILED_USER_RESTRICTED", "系统限制了这个用户卸载"),
    ("DELETE_FAILED_OWNERSHIP", "应用属于其他用户，当前用户不能卸载"),
    ("USER_RESTRICTED", "系统限制了当前用户的操作"),
    ("is not installed", "设备上没装这个应用"),
    ("Unknown package", "设备上没装这个应用"),
    ("Permission Denial", "没有权限，应用受系统保护"),
    ("SecurityException", "系统安全策略拒绝了这个操作"),
    ("device offline", "设备掉线了，请重新连接"),
    ("No such file", "找不到文件"),
    ("Operation not permitted", "系统不允许这个操作"),
    ("Command not found", "设备不认识这条命令"),
    ("Broken pipe", "系统服务没响应（可能是系统应用被停用导致），重启设备可恢复"),
    ("Failure calling service", "系统服务没响应，重启设备可恢复"),
    ("Killed", "操作被系统终止，可能被安全策略拦截"),
    ("Bad argument", "参数不被设备接受"),
    ("unknown command", "设备不认识这条命令"),
    ("INSTALL_FAILED_INSUFFICIENT_STORAGE", "设备存储空间不够"),
    ("INSTALL_FAILED_ALREADY_EXISTS", "设备上已装同名应用且签名不同，需要先卸载"),
    ("INSTALL_FAILED_NO_MATCHING_ABIS", "这个包和设备的 CPU 架构不匹配"),
    ("INSTALL_FAILED_OLDER_SDK", "这个包要求的安卓版本比设备高"),
    ("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "签名和设备上已装的不一致，需要先卸载旧版"),
    ("INSTALL_FAILED_VERIFICATION_FAILURE", "被系统的安装校验拦住了"),
    ("INSTALL_FAILED_USER_RESTRICTED", "系统限制了安装（常见于电视的儿童锁或安装限制）"),
    ("Failure [", "系统拒绝了这次操作："),
]


def action_reason(text):
    """把 adb 英文报错换成人话。"""
    raw = (text or "").strip()
    low = raw.lower()
    for key, cn in ACTION_ERRORS:
        if key.lower() in low:
            return cn
    return raw


def install_reason(out):
    text = out or ""
    if "UPDATE_INCOMPATIBLE" in text:
        return "签名和电视上已装的不一致，已自动卸载旧包重装"
    if "VERSION_DOWNGRADE" in text:
        return "新包版本号比电视上已装的低"
    if "OLDER_SDK" in text or "NO_MATCHING_ABIS" in text:
        return "这个包和电视的安卓版本或 CPU 不匹配"
    if "INSTALL_PARSE_FAILED" in text or "NOT_APK" in text:
        return "文件不是完整 APK，下载可能中断"
    if "device offline" in text or "not found" in text:
        return "安装过程中电视断开了"
    mapped = action_reason(text)
    if mapped != text:
        return mapped
    tail = [line.strip() for line in text.splitlines() if line.strip()]
    return tail[-1] if tail else "adb 没返回原因"


def post_json(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    urllib.request.urlopen(req, timeout=15).read()


def send_notice(cfg, title, text):
    sent = []
    urls = cfg.get("notify") or {}
    if isinstance(urls, dict) and urls.get("url"):
        post_json(urls["url"], {"title": title, "body": text})
        sent.append("自定义")
    if isinstance(urls, dict) and urls.get("feishu"):
        post_json(urls["feishu"], {"msg_type": "text", "content": {"text": f"{title}\n{text}"}})
        sent.append("飞书")
    if isinstance(urls, dict) and urls.get("pushplus"):
        post_json("https://www.pushplus.plus/send", {"token": urls["pushplus"], "title": title, "content": text})
        sent.append("PushPlus")
    if isinstance(urls, dict) and urls.get("bark"):
        base = urls["bark"].rstrip("/")
        req = urllib.request.Request(
            f"{base}/{urllib.request.quote(title)}/{urllib.request.quote(text)}",
            headers={"User-Agent": UA},
        )
        urllib.request.urlopen(req, timeout=15).read()
        sent.append("Bark")
    return sent


def notice(cfg, title, text):
    try:
        sent = send_notice(cfg, title, text)
        log(("已通知 " + "/".join(sent) if sent else "通知未配置") + f"：{title}")
    except Exception as exc:
        log(f"通知失败 {exc}")


def device_label(address):
    for item in current_config().get("devices") or []:
        if device_address(item) == address:
            return item.get("name") or address
    return address


def device_address(device):
    if device.get("address"):
        return device["address"]
    return f"{str(device.get('ip', '')).strip()}:{int(device.get('port') or 5555)}"


def known_address(address):
    return address in {device_address(item) for item in current_config().get("devices") or []}


def app_labels_all(address):
    """一次 dumpsys 拿到所有包的 label/version 映射，避免逐包调用。"""
    code, out = adb("-s", address, "shell", "dumpsys", "package", timeout=120)
    if code != 0 or not out:
        return {}
    table = {}
    cur = ""
    for line in out.splitlines():
        m = re.match(r"\s*Package \[([^\]]+)\]", line)
        if m:
            cur = m.group(1).strip()
            continue
        if not cur:
            continue
        lm = re.search(r"application-label(?:-zh)?:'([^']*)'", line)
        if lm and cur not in table:
            table.setdefault(cur, {})["label"] = lm.group(1)
        vm = re.search(r"versionName=(\S+)", line)
        if vm and "version" not in table.get(cur, {}):
            table.setdefault(cur, {})["version"] = vm.group(1)
    return table


_LABEL_CACHE = {}


def app_label(address, package):
    code, out = adb("-s", address, "shell", "dumpsys", "package", package, timeout=20)
    label = re.search(r"application-label(?:-zh)?:'([^']*)'", out)
    version = re.search(r"versionName=(\S+)", out)
    ver = version.group(1) if version else ""
    lab = label.group(1) if label else ""
    if lab:
        return lab, ver
    # dumpsys 没有 label（不少 TV 应用如此）：把 APK 拉回本地用容器内 aapt 读 badging
    code, path = adb("-s", address, "shell", "pm", "path", package, timeout=15)
    m = re.search(r"package:(\S+\.apk)", path)
    if not m:
        return "", ver
    apk_path = m.group(1)
    cache_key = f"{address}|{apk_path}"
    if cache_key in _LABEL_CACHE:
        lab, cver = _LABEL_CACHE[cache_key]
        return lab, (ver or cver)
    with tempfile.TemporaryDirectory() as td:
        apk_local = Path(td) / "app.apk"
        code, _ = adb("-s", address, "pull", apk_path, str(apk_local), timeout=90)
        if not apk_local.exists():
            return "", ver
        try:
            proc = subprocess.run(["aapt", "dump", "badging", str(apk_local)], capture_output=True, text=True, timeout=30)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception:
            return "", ver
    lab = re.search(r"application-label(?:-zh)?:'([^']*)'", out)
    v2 = re.search(r"versionName='([^']*)'", out)
    if v2 and not ver:
        ver = v2.group(1)
    label_text = lab.group(1) if lab else ""
    _LABEL_CACHE[cache_key] = (label_text, ver)
    return label_text, ver


def source_of(app):
    return (app.get("source") or app.get("repo") or "").strip()


def github_repo(source):
    text = source.strip().rstrip("/")
    path = text.lower().split("?", 1)[0]
    if ".apk" in path and "/blob/" not in path and "/raw/" not in path:
        return ""
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", text)
    if match:
        repo = match.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        rest = text.split(match.group(0), 1)[1].lstrip("/")
        if rest and not rest.startswith(("releases", "tags")):
            return ""
        return f"{match.group(1)}/{repo}"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        return text
    return ""


def remote_size(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return int(resp.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def apk_identity(path):
    code, out = adb_tool("aapt", "dump", "badging", str(path), timeout=30)
    if code != 0:
        return {"package": "", "version": "", "label": ""}
    pkg = re.search(r"package: name='([^']+)'", out)
    ver = re.search(r"versionName='([^']*)'", out)
    label = re.search(r"application-label:'([^']*)'", out)
    return {
        "package": pkg.group(1) if pkg else "",
        "version": ver.group(1) if ver else "",
        "label": label.group(1) if label else "",
    }


def cache_file(rel):
    root = CACHE.resolve()
    target = (CACHE / rel).resolve()
    if not str(target).startswith(str(root) + os.sep) or not target.is_file() or target.suffix.lower() != ".apk":
        return None
    return target


def cache_rows():
    if not CACHE.exists():
        return []
    rows = []
    for path in sorted(CACHE.rglob("*.apk")):
        if not path.is_file():
            continue
        ident = apk_identity(path)
        rel = path.relative_to(CACHE).as_posix()
        rows.append({
            "id": rel,
            "file": path.name,
            "folder": path.parent.relative_to(CACHE).as_posix() if path.parent != CACHE else "",
            "size": path.stat().st_size,
            "package": ident["package"],
            "version": ident["version"],
            "label": ident["label"],
        })
    return rows


def adb_tool(*args, timeout=30):
    proc = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def release_shape(name):
    text = re.sub(r"\d+(?:\.\d+)+", "#", name)
    text = re.sub(r"(?<=[-_])v(?=#)", "", text)
    text = re.sub(r"(?i)(?:^|[-_])(?:arm64-v8a|armeabi-v7a|arm64|aarch64|armeabi|x86_64|x86)(?=[-_.]|$)", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text


def asset_payload(item):
    return {"name": item["name"], "url": item["browser_download_url"], "size": item.get("size") or 0}


def release_groups(releases):
    groups = []
    for release in releases:
        if release.get("prerelease"):
            continue
        assets = [item for item in (release.get("assets") or []) if str(item.get("name", "")).lower().endswith(".apk")]
        buckets = {}
        for item in assets:
            buckets.setdefault(release_shape(item["name"]), []).append(item)
        for shape, items in buckets.items():
            groups.append({"tag": release.get("tag_name") or "", "shape": shape, "assets": items})
    return groups


def choose_group(groups):
    if not groups:
        return None
    if len(groups) == 1:
        return groups[0]
    tv = [item for item in groups if re.search(r"(?i)(^|[^a-z])tv([^a-z]|$)", item["shape"])]
    return tv[0] if tv else groups[0]


def release_line(source):
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)/releases/download/([^/\s]+)/([^?\s]+)", source.strip())
    if not match or not match.group(4).lower().split("?")[0].endswith(".apk"):
        return None
    repo = match.group(2)[:-4] if match.group(2).endswith(".git") else match.group(2)
    return {
        "repo": f"{match.group(1)}/{repo}",
        "tag": match.group(3),
        "name": match.group(4).split("?")[0],
        "shape": release_shape(match.group(4).split("?")[0]),
    }


def resolve_release(source, family):
    line = release_line(source)
    repo = line["repo"] if line else github_repo(source)
    if repo:
        releases = http_json(f"https://api.github.com/repos/{repo}/releases?per_page=30")
        if line:
            groups = [item for item in release_groups(releases) if item["shape"] == line["shape"]]
            group = groups[0] if groups else None
            if not group:
                return {"key": repo + "|" + line["shape"], "tag": "", "error": f"{repo} 没有找到和 {line['name']} 同一类的更新"}
        else:
            group = choose_group(release_groups(releases))
            if not group:
                return {"key": repo, "tag": "", "error": f"{repo} 没有 APK"}
        assets = arm_assets(group["assets"])
        if not assets:
            return {"key": repo, "tag": group["tag"], "error": f"{repo} {group['tag']} 没有 APK"}
        key = repo if not line else f"{repo}|{line['shape']}"
        return {
            "key": key,
            "tag": group["tag"],
            "name": assets[0]["name"],
            "url": assets[0]["browser_download_url"],
            "size": assets[0].get("size") or 0,
            "assets": [asset_payload(item) for item in assets],
        }
    if not source.lower().split("?", 1)[0].endswith(".apk"):
        return {"key": source, "tag": "", "error": f"{source} 不是 GitHub 仓库，也不是 APK 直链"}
    name = urllib.parse.unquote(source.rstrip("/").split("/")[-1].split("?")[0]) or "app.apk"
    url = source.replace("/blob/", "/raw/")
    siblings = directory_apks(url)
    same = [item for item in siblings if release_shape(item["name"]) == release_shape(name)]
    files = arm_assets(same) or [{"name": name, "browser_download_url": url, "size": remote_size(url)}]
    return {
        "key": source,
        "tag": name,
        "name": files[0]["name"],
        "url": files[0]["browser_download_url"],
        "size": files[0].get("size") or 0,
        "assets": [asset_payload(item) for item in files],
    }


def directory_apks(url):
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    path = urllib.parse.unquote(parts.path)
    if host != "github.com" or "/raw/" not in path:
        return []
    match = re.match(r"^/([^/]+)/([^/]+)/raw/([^/]+)(/.*)?$", path)
    if not match:
        return []
    owner, repo, ref, folder = match.group(1), match.group(2), match.group(3), match.group(4)
    folder = (folder or "/").rsplit("/", 1)[0] or "/"
    api = f"https://api.github.com/repos/{owner}/{repo}/contents{urllib.parse.quote(folder)}?ref={ref}"
    try:
        rows = http_json(api)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    files = []
    for row in rows:
        if row.get("type") != "file" or not str(row.get("name", "")).lower().endswith(".apk"):
            continue
        files.append({"name": row["name"], "browser_download_url": row.get("download_url") or "", "size": row.get("size") or 0})
    return files


def prune_old_apks(source, files, tag, label):
    kept = set()
    for item in files:
        path = cache_dest(source, tag, item["name"])
        if path.exists():
            kept.add(path.name)
    folder = CACHE / cache_key(source)
    if not kept or not folder.exists():
        return
    for old in folder.glob("*.apk"):
        if old.name not in kept:
            old.unlink()
            log(f"{label} 删除旧包 {old.name}")


def once(force=False):
    # 第①步：读取配置。没配置设备/软件就不跑。
    cfg = current_config()
    state = load_json(STATE, {"apps": {}, "checked_at": ""})
    notify_ok = {}
    notify_fail = {}
    changed = False
    devices = [item for item in (cfg.get("devices") or []) if device_address(item).split(":")[0]]
    apps = [item for item in (cfg.get("apps") or []) if source_of(item)]
    if not devices or not apps:
        log("还没有设备和软件")
        return

    now = datetime.now()
    offline = state.get("offline") or {}
    cooldown_s = max(1, int(cfg.get("install_minutes") or 10)) * 60

    # 第②步：到时间才查在线链接、下载最新安装包。本地有就不下，本地没有才下。
    checked = state.get("checked_at") or ""
    due = True
    if checked and not force:
        try:
            age = now - datetime.fromisoformat(checked)
            due = age.total_seconds() >= max(1, int(cfg.get("pull_hours") or 24)) * 3600
        except ValueError:
            due = True
    chosen_map = {}
    dl_count = 0
    if due:
        for app in apps:
            source = source_of(app)
            label = app.get("name") or source
            if source.startswith("file:"):
                continue
            try:
                chosen = resolve_release(source, "")
            except urllib.error.HTTPError as exc:
                log(f"{label} 查版本失败 HTTP {exc.code}")
                continue
            except Exception as exc:
                log(f"{label} 查版本失败 {exc}")
                continue
            if chosen.get("error"):
                log(f"{label} {chosen['error']}")
                continue
            chosen_map[source] = chosen
            files = chosen.get("assets") or [{"name": chosen["name"], "url": chosen["url"], "size": chosen["size"]}]
            downloaded = False
            for item in files:
                path = cache_dest(source, chosen["tag"], item["name"])
                if not path.exists() or (item["size"] and path.stat().st_size != item["size"]):
                    try:
                        download(item["url"], path)
                        downloaded = True
                        dl_count += 1
                        log(f"下载 {label} {item['name']}")
                    except Exception as exc:
                        log(f"{label} 下载失败 {exc}")
                        continue
            if downloaded:
                prune_old_apks(source, files, chosen["tag"], label)
        state["checked_at"] = now.isoformat(timespec="seconds")
        save_json(STATE, state)

    # 第③步：拿本地安装包 vs 本地记录(state) 交叉对比。
    # 只对「本地包比记录新 / 记录缺失」的软件，才需要进第④步去连设备。否则忽略，本轮结束。
    need_install = {}  # source -> {app, cached, chosen}
    for app in apps:
        source = source_of(app)
        label = app.get("name") or source
        # 查找该 source 的本地缓存包：不依赖目录名，直接在全缓存里匹配 source 关键字。
        cached = []
        if CACHE.exists():
            if source.startswith("file:"):
                want = source.split(":", 1)[-1]
                cached = [item for item in CACHE.rglob("*.apk") if item.is_file() and item.name == want]
            else:
                repo = github_repo(source)
                base = urllib.parse.unquote(source.strip().split("?", 1)[0].rstrip("/").split("/")[-1])
                hint = (repo or "").replace("/", "_") if repo else base.replace(".apk", "")
                for item in CACHE.rglob("*.apk"):
                    if not item.is_file():
                        continue
                    if (hint and (hint in item.parent.name or hint in item.name)):
                        cached.append(item)
        if not cached:
            continue
        chosen = {}
        if source.startswith("file:"):
            chosen = {"key": source, "tag": "", "name": source.split(":", 1)[-1]}
        else:
            chosen = chosen_map.get(source) or {"key": source, "tag": ""}
        # 第③步预判：本地包 vs 本地记录。记录里已是最新就不连设备，本轮跳过。
        # 用本地第一个包解析版本，和该 source 在 state 里已知的最高版本比。
        local_ver = ""
        try:
            for c in cached:
                v = apk_identity(c).get("version") or ""
                if v and (not local_ver or version_ge(v, local_ver)):
                    local_ver = v
        except Exception:
            pass
        recorded_ver = ""
        for device in devices:
            address = device_address(device)
            candidates = []
            if chosen.get("key"):
                candidates.append(f"{address}|{chosen['key']}")
            candidates.append(f"{address}|{source}")
            for ckey in candidates:
                rec = state["apps"].get(ckey, {})
                rv = rec.get("installed") or rec.get("version") or ""
                if rv and (not recorded_ver or version_ge(rv, recorded_ver)):
                    recorded_ver = rv
        if local_ver and recorded_ver and not version_gt(local_ver, recorded_ver):
            # 本地包未严格新于记录版本（记录已是最新或更新），本轮无需连设备安装
            continue
        need_install[source] = {"app": app, "label": label, "cached": cached, "chosen": chosen}

    # 没软件需要动设备，本轮结束，不写噪音日志。
    if not need_install:
        if dl_count:
            return f"已下载 {dl_count} 个新包，设备无需安装"
        return "本轮没有需要更新的软件"

    # 第④步：只对需要安装的软件，连接设备对比实装版本。
    profiles = {}
    for source, item in need_install.items():
        app = item["app"]
        label = item["label"]
        chosen = item["chosen"]
        for device in devices:
            address = device_address(device)
            name = device.get("name", address)
            if address not in profiles:
                last_fail = offline.get(address)
                if last_fail:
                    try:
                        if (now - datetime.fromisoformat(last_fail)).total_seconds() < cooldown_s:
                            continue
                    except ValueError:
                        pass
                profile, err = device_profile(address)
                if err:
                    offline[address] = now.isoformat(timespec="seconds")
                    log(f"{name} 不在线，跳过")
                    continue
                offline.pop(address, None)
                profiles[address] = profile
                device["model"] = profile["model"]
                device["soc"] = profile.get("soc", "")
                device["cores"] = profile.get("cores", "")
                device["abi"] = profile["abi"]
                device["android"] = profile["release"]
                device["seen_at"] = now.isoformat(timespec="seconds")
                log(f"{now.strftime('%H:%M')} {name} 在线，检测到 {profile['model']}")
            profile = profiles.get(address)
            if not profile:
                continue
            min_sdk = int(app.get("min_sdk") or 0)
            if min_sdk and profile["sdk"] and profile["sdk"] < min_sdk:
                continue
            key = f"{address}|{chosen['key']}" if chosen.get("key") else f"{address}|{source}"
            wanted = source.split(":", 1)[-1] if source.startswith("file:") else None
            pool = [fl for fl in item["cached"] if (not wanted or fl.name == wanted)]
            picked = asset_for_device([{"name": x.name, "path": x} for x in pool], profile["family"])
            dest = picked["path"] if picked else None
            if dest is None or not dest.exists():
                continue
            ident = apk_identity(dest)
            if ident["package"]:
                app["package"] = ident["package"]
                app["apk_version"] = ident["version"]
                app["apk_label"] = ident["label"]
                save_config(cfg)
            installed = installed_version(address, ident["package"]) if ident["package"] else ""
            prev = state["apps"].get(key, {})
            if installed:
                state["apps"].setdefault(key, {}).update({"installed": installed, "seen_at": now.isoformat(timespec="seconds")})
            # 设备已装版本 >= 本地包版本：记录没问题就只备份记录，不重复安装
            if installed and ident["version"] and version_ge(installed, ident["version"]):
                state["apps"][key] = {"asset": dest.name, "tag": chosen.get("tag"), "version": ident["version"], "installed": installed, "at": now.isoformat(timespec="seconds")}
                changed = True
                log(f"{now.strftime('%H:%M')} {name} {label} 已是 {installed}，跳过")
                continue
            code, out = adb("-s", address, "install", "-r", str(dest), timeout=300)
            if code != 0 and "UPDATE_INCOMPATIBLE" in out:
                # 签名和已装包不一致,直接覆盖装不上。自动卸载旧的再装新的,不丢这一步
                log(f"{now.strftime('%H:%M')} {name} {label} 签名不一致，自动先卸载旧包再装")
                adb("-s", address, "shell", "pm", "uninstall", "--user", "0", ident["package"], timeout=60)
                code, out = adb("-s", address, "install", "-r", str(dest), timeout=300)
            shown = chosen.get("tag") or dest.name
            if code == 0 and "Success" in out:
                state["apps"][key] = {
                    "tag": chosen.get("tag"),
                    "asset": dest.name,
                    "version": ident["version"],
                    "installed": ident["version"],
                    "at": now.isoformat(timespec="seconds"),
                }
                changed = True
                from_v = prev.get("installed") or prev.get("version") or "无"
                to_v = ident["version"]
                ip = address.rsplit(":", 1)[0] if ":" in address else address
                notify_ok.setdefault(name, []).append(f"{name} {ip}｜{label} {from_v}→{to_v} 成功")
                log(f"{now.strftime('%H:%M')} {name} {label} 已装 {shown}")
            else:
                from_v = prev.get("installed") or installed or "无"
                to_v = ident["version"] or "?"
                ip = address.rsplit(":", 1)[0] if ":" in address else address
                notify_fail.setdefault(name, []).append(f"{name} {ip}｜{label} {from_v}→{to_v} 失败：{install_reason(out)}")
                log(f"{now.strftime('%H:%M')} {name} {label} 安装失败：{install_reason(out)}")
    if offline:
        state["offline"] = offline
    if changed or offline:
        save_json(STATE, state)

    # 第⑤步：只报安装结果。成功列设备+软件+版本变化，失败列设备+软件+原因。没安装不通知。
    dl_line = f"本轮已下载 {dl_count} 个新包" if dl_count else ""
    ok_text = "\n".join(item for items in notify_ok.values() for item in items)
    fail_text = "\n".join(item for items in notify_fail.values() for item in items)
    if ok_text and fail_text:
        notice(cfg, "部分更新完成", f"成功：\n{ok_text}\n\n失败：\n{fail_text}")
        return f"部分更新完成\n成功：\n{ok_text}\n\n失败：\n{fail_text}"
    elif fail_text:
        notice(cfg, "更新失败", fail_text)
        return f"更新失败\n{fail_text}"
    elif ok_text:
        notice(cfg, "更新完成", ok_text)
        return f"更新完成\n{ok_text}"
    if dl_line:
        return f"{dl_line}，设备未在线或无需安装"
    return "本轮没有需要更新的软件"
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send(self, code, body, content="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        size = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(size).decode("utf-8") or "{}")

    def do_GET(self):
        if self.path == "/":
            self.send(200, PAGE_FILE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif self.path in ("/logo.png", "/favicon.png"):
            icon = PAGE_FILE.with_name("assets") / self.path.lstrip("/")
            self.send(200, icon.read_bytes(), "image/png")
        elif self.path == "/api/config":
            self.send(200, json.dumps(current_config(), ensure_ascii=False))
        elif self.path == "/api/config.yaml":
            self.send(200, CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else dump_yaml(DEFAULT), "application/yaml; charset=utf-8")
        elif self.path.startswith("/api/packages"):
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            address = (query.get("address") or [""])[0]
            if not known_address(address):
                self.send(400, "[]")
                return
            ok, _detail = connect(address)
            if not ok:
                self.send(200, json.dumps({"offline": True, "rows": []}, ensure_ascii=False))
                return
            kind = (query.get("kind") or ["user"])[0]
            with_labels = (query.get("labels") or ["1"])[0] != "0"
            flag = "-3" if kind == "user" else "-s"
            code, out = adb("-s", address, "shell", "pm", "list", "packages", "-f", flag, timeout=30)
            parsed = []
            for line in out.splitlines():
                if "package:" not in line:
                    continue
                body = line.split("package:", 1)[-1]
                path, pkg = body.rsplit("=", 1)
                parsed.append((pkg.strip(), path))
            rows = []
            def _labels(pkg):
                lab, ver = app_label(address, pkg)
                return pkg, lab, ver
            if with_labels:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    info = {r[0]: (r[1], r[2]) for r in pool.map(_labels, [p for p, _ in parsed])}
            else:
                # 快速模式：只并行取版本号（不读 APK），名称留空由第二阶段补
                def _ver(pkg):
                    code, out = adb("-s", address, "shell", "dumpsys", "package", pkg, timeout=20)
                    m = re.search(r"versionName=(\S+)", out or "")
                    return pkg, (m.group(1) if m else "")
                with ThreadPoolExecutor(max_workers=8) as pool:
                    info = {pkg: ("", ver) for pkg, ver in pool.map(_ver, [p for p, _ in parsed])}
            for pkg, path in parsed:
                lab, ver = info.get(pkg, ("", ""))
                rows.append({"package": pkg, "version": ver, "label": lab, "path": path})
            rows.sort(key=lambda item: item["package"].lower())
            self.send(200, json.dumps(rows, ensure_ascii=False))
        elif self.path.startswith("/api/processes"):
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            address = (query.get("address") or [""])[0]
            if not known_address(address):
                self.send(400, "[]")
                return
            ok, _detail = connect(address)
            if not ok:
                self.send(200, json.dumps({"offline": True, "rows": []}, ensure_ascii=False))
                return
            code, out = adb("-s", address, "shell", "ps", "-A", "-o", "USER,PID,RSS,NAME", timeout=20)
            # 已装应用的包名→名称映射（进程名多为包名，直接查表给中文/真实名称）
            label_map = {}
            code, pkgs = adb("-s", address, "shell", "pm", "list", "packages", timeout=25)
            for line in (pkgs or "").splitlines():
                if "package:" in line:
                    label_map[line.split("package:", 1)[-1].strip()] = ""
            rows=[]
            for line in out.splitlines()[1:]:
                parts=line.split()
                if len(parts) < 4 or not parts[1].isdigit():
                    continue
                rss=int(parts[2]) if parts[2].isdigit() else 0
                name=parts[-1]
                rows.append({"user": parts[0], "pid": parts[1], "rss": rss, "name": name})
            # 只对出现在进程里的包名补名称（数量有限，避免整机遍历）
            need = [r["name"].split(":")[0] for r in rows if r["name"].split(":")[0] in label_map]
            seen = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                for pkg, lab, _ver in pool.map(lambda p: (p,) + app_label(address, p), sorted(set(need))):
                    seen[pkg] = lab
            for r in rows:
                base = r["name"].split(":")[0]
                r["label"] = seen.get(base, "") or ""
            rows.sort(key=lambda item: item["rss"], reverse=True)
            self.send(200, json.dumps(rows, ensure_ascii=False))
        elif self.path == "/api/devices":
            rows = []
            state = load_json(STATE, {"apps": {}, "offline": {}})
            offline = state.get("offline") or {}
            now = datetime.now()
            cooldown_s = max(1, int(current_config().get("install_minutes") or 10)) * 60
            for device in current_config().get("devices") or []:
                address = device_address(device)
                if not address.split(":")[0]:
                    continue
                row = {"name": device.get("name") or address, "ip": device.get("ip", ""), "address": address}
                # 冷却期内不重试,直接显示离线,避免每次刷新白等 connect 20s
                last_fail = offline.get(address)
                if last_fail:
                    try:
                        if (now - datetime.fromisoformat(last_fail)).total_seconds() < cooldown_s:
                            row.update({"online": False, "detail": "离线(冷却中，稍后重试)"})
                            rows.append(row)
                            continue
                    except ValueError:
                        pass
                profile, err = device_profile(address)
                if profile:
                    row.update({"online": True, "model": profile["model"], "android": profile["release"], "abi": profile["abi"], "soc": profile.get("soc", ""), "cores": profile.get("cores", ""), "sdk": profile["sdk"]})
                    row.update(device_stats(address))
                else:
                    row.update({"online": False, "detail": err or "没开机或 5555 没开"})
                rows.append(row)
            # 注意: 不在 GET 里写 state.json。once() 循环会维护 offline 冷却,
            # GET 处理器写 .tmp 会与主循环并发竞争导致 FileNotFoundError。
            self.send(200, json.dumps(rows, ensure_ascii=False))
        elif self.path == "/api/log":
            lines = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
            text = "\n".join(reversed(lines[-50:])) if lines else "还没有日志"
            self.send(200, text, "text/plain; charset=utf-8")
        elif self.path.startswith("/api/cache"):
            from urllib.parse import urlparse, parse_qs, unquote
            query = parse_qs(urlparse(self.path).query)
            rel = unquote((query.get("id") or [""])[0])
            if rel:
                path = cache_file(rel)
                if not path:
                    self.send(404, "文件不在缓存里", "text/plain; charset=utf-8")
                    return
                data = path.read_bytes()
                self.send(200, data, "application/vnd.android.package-archive")
                return
            self.send(200, json.dumps(cache_rows(), ensure_ascii=False))
        elif self.path == "/api/pull-status":
            self.send(200, json.dumps(PULL, ensure_ascii=False))
        else:
            self.send(404, "{}")

    def do_POST(self):
        try:
            if self.path == "/api/devices-refresh":
                cfg = current_config()
                state = load_json(STATE, {"apps": {}, "offline": {}})
                offline = state.get("offline") or {}
                rows = []
                for device in cfg.get("devices") or []:
                    address = device_address(device)
                    if not address.split(":")[0]:
                        continue
                    row = {"name": device.get("name") or address, "ip": device.get("ip", ""), "address": address}
                    profile, err = device_profile(address)
                    if profile:
                        offline.pop(address, None)
                        row.update({"online": True, "model": profile["model"], "android": profile["release"], "abi": profile["abi"], "soc": profile.get("soc", ""), "cores": profile.get("cores", ""), "sdk": profile["sdk"]})
                        row.update(device_stats(address))
                    else:
                        offline[address] = datetime.now().isoformat(timespec="seconds")
                        row.update({"online": False, "detail": err or "没开机或 5555 没开"})
                    rows.append(row)
                state["offline"] = offline
                save_json(STATE, state)
                self.send(200, json.dumps(rows, ensure_ascii=False))
                return
            if self.path == "/api/upload":
                added, error = save_uploads(self)
                self.send(200, json.dumps({"added": added, "error": error}, ensure_ascii=False))
                return
            if self.path == "/api/config":
                incoming = self.read_json()
                incoming.pop("github_token", None)
                kept_devices = []
                for device in incoming.get("devices") or []:
                    ip = str(device.get("ip") or "").strip()
                    if not ip:
                        continue  # 空 IP 的设备不保存,避免生成 ":5555" 非法地址
                    device["port"] = int(device.get("port") or 5555)
                    device["address"] = device_address(device)
                    kept_devices.append(device)
                incoming["devices"] = kept_devices
                incoming["pull_hours"] = max(1, int(incoming.get("pull_hours") or 24))
                incoming["install_minutes"] = max(1, int(incoming.get("install_minutes") or 10))
                before = current_config()
                save_config(incoming)
                for added in [d for d in incoming["devices"] if device_address(d) not in {device_address(x) for x in before.get("devices") or []}]:
                    log(f"添加设备 {added.get('name') or device_address(added)}")
                for gone in [d for d in before.get("devices") or [] if device_address(d) not in {device_address(x) for x in incoming["devices"]}]:
                    log(f"删除设备 {gone.get('name') or device_address(gone)}")
                old_src = [source_of(a) for a in before.get("apps") or []]
                for app in incoming.get("apps") or []:
                    if source_of(app) not in old_src:
                        log(f"添加链接 {app.get('name') or source_of(app)}")
                new_src = [source_of(a) for a in incoming.get("apps") or []]
                for app in before.get("apps") or []:
                    if source_of(app) not in new_src:
                        log(f"删除链接 {app.get('name') or source_of(app)}")
                self.send(200, "{\"ok\":true}")
            elif self.path == "/api/notify-test":
                sent = send_notice(current_config(), "电视装包测试", "通知通道可用")
                self.send(200, "已发到：" + (" / ".join(sent) if sent else "没有填写通知"), "text/plain; charset=utf-8")
            elif self.path == "/api/shell":
                body = self.read_json()
                address = body.get("address","")
                command = (body.get("command") or "").strip()
                if not known_address(address) or not command or command.startswith("adb"):
                    self.send(400, "填已保存的设备和命令。不要再加 adb 前缀", "text/plain; charset=utf-8")
                    return
                code, out = adb("-s", address, "shell", command, timeout=30)
                result = (out or "").strip() or ("成功" if code == 0 else f"退出码 {code}")
                log(f"{device_label(address)} 执行命令 {command}")
                self.send(200, result, "text/plain; charset=utf-8")
            elif self.path == "/api/app-action":
                body = self.read_json()
                address = body.get("address", "")
                package = (body.get("package") or "").strip()
                action = body.get("action", "")
                if not known_address(address) or not re.fullmatch(r"[A-Za-z0-9._]+", package):
                    self.send(400, "设备或包名不对", "text/plain; charset=utf-8")
                    return
                act_names = {"uninstall": "卸载", "clear": "清除数据", "disable": "停用", "enable": "启用", "stop": "强制停止", "cache": "清缓存"}
                commands = {
                    "uninstall": ["pm", "uninstall", "--user", "0", package],
                    "clear": ["pm", "clear", package],
                    "disable": ["pm", "disable-user", "--user", "0", package],
                    "enable": ["pm", "enable", package],
                    "stop": ["am", "force-stop", package],
                }
                cache_ok = False
                if action == "cache":
                    code, out = adb("-s", address, "shell", "pm", "clear", "--cache-only", package, timeout=30)
                    text = (out or "").strip()
                    if code == 0 and "Success" in text:
                        cache_ok = True
                    else:
                        code, out = adb("-s", address, "shell", "cmd", "package", "trim-caches", "999999999999", timeout=30)
                        cache_ok = code == 0
                elif action in commands:
                    code, out = adb("-s", address, "shell", *commands[action], timeout=40)
                else:
                    self.send(400, "不支持这个操作", "text/plain; charset=utf-8")
                    return
                raw = (out or "").strip()
                low = raw.lower()
                ok_text = ("success" in low) or ("new state:" in low) or raw == ""
                if action == "cache":
                    result = "成功" if cache_ok else "失败：" + (action_reason(raw) or "退出码 " + str(code))
                elif code == 0 and ok_text and "error" not in low and "not found" not in low:
                    result = "成功"
                else:
                    result = "失败：" + (action_reason(raw) or "退出码 " + str(code))
                log(f"{device_label(address)} {act_names.get(action, action)} {package}：{result}")
                self.send(200, result, "text/plain; charset=utf-8")
            elif self.path == "/api/kill":
                body = self.read_json()
                address = body.get("address", "")
                pid = str(body.get("pid") or "")
                name = (body.get("name") or "").strip()
                if not known_address(address) or not pid.isdigit():
                    self.send(400, "设备或进程不对", "text/plain; charset=utf-8")
                    return
                if name and "." in name:
                    code, out = adb("-s", address, "shell", "am", "force-stop", name, timeout=20)
                else:
                    code, out = adb("-s", address, "shell", "kill", pid, timeout=20)
                log(f"{device_label(address)} 停止进程 {name or pid}")
                self.send(200, (out or "已停止").strip(), "text/plain; charset=utf-8")
            elif self.path == "/api/cache-action":
                body = self.read_json()
                path = cache_file(body.get("id") or "")
                action = body.get("action")
                if not path:
                    self.send(400, "文件不在缓存里", "text/plain; charset=utf-8")
                    return
                if action == "delete":
                    path.unlink()
                    log(f"删除软件包 {path.name}")
                    self.send(200, "已删除", "text/plain; charset=utf-8")
                    return
                if action == "push":
                    address = body.get("address") or ""
                    if not known_address(address):
                        self.send(400, "先选一台已保存的设备", "text/plain; charset=utf-8")
                        return
                    code, out = adb("-s", address, "install", "-r", str(path), timeout=300)
                    if code != 0 and "UPDATE_INCOMPATIBLE" in out:
                        ident = apk_identity(path)
                        pkg = ident.get("package") or ""
                        if pkg:
                            adb("-s", address, "shell", "pm", "uninstall", "--user", "0", pkg, timeout=60)
                            code, out = adb("-s", address, "install", "-r", str(path), timeout=300)
                    name = device_label(address)
                    if code == 0 and "Success" in out:
                        log(f"手动推送 {path.name} → {name} 成功")
                        self.send(200, "已推送到设备", "text/plain; charset=utf-8")
                    else:
                        reason = install_reason(out)
                        log(f"手动推送 {path.name} → {name} 失败：{reason}")
                        self.send(200, reason, "text/plain; charset=utf-8")
                    return
                self.send(400, "不支持这个操作", "text/plain; charset=utf-8")
            elif self.path == "/api/pull":
                if PULL["running"]:
                    self.send(200, "已有一轮拉取在跑，稍等", "text/plain; charset=utf-8")
                    return
                PULL["running"] = True
                PULL["result"] = ""

                def _run_pull():
                    try:
                        with LOCK:
                            PULL["result"] = once(force=True) or "已检查一轮"
                    except Exception as exc:
                        PULL["result"] = f"本轮失败 {exc}"
                    finally:
                        PULL["running"] = False

                threading.Thread(target=_run_pull, daemon=True).start()
                self.send(200, "已开始拉取，看下方日志看进度", "text/plain; charset=utf-8")
            else:
                self.send(404, "{}")
        except Exception as exc:
            self.send(400, str(exc), "text/plain; charset=utf-8")


def save_uploads(handler):
    import email
    from email.policy import default
    added = []
    length = int(handler.headers.get("Content-Length") or 0)
    content_type = handler.headers.get("Content-Type") or ""
    raw = handler.rfile.read(length)
    if "multipart/form-data" not in content_type:
        return [], "不是上传文件"
    message = email.message_from_bytes(b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw, policy=default)
    CACHE.mkdir(parents=True, exist_ok=True)
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = Path(part.get_filename() or "").name
        if not name.lower().endswith(".apk"):
            continue
        dest = CACHE / name
        payload = part.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)):
            continue
        dest.write_bytes(payload)
        try:
            ident = apk_identity(dest)
        except Exception:
            ident = {"package": "", "version": "", "label": ""}
        added.append({"name": ident["label"] or name[:-4], "source": f"file:{name}", "package": ident["package"]})
    if not added:
        return [], "没有 APK"
    return added, ""


def migrate_cache_dirs(cfg):
    for app in cfg.get("apps") or []:
        source = source_of(app)
        if not source or github_repo(source):
            continue
        old_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.strip().split("?", 1)[0].rstrip("/").split("/")[-1])[:80]
        new_dir = CACHE / cache_key(source)
        old_dir = CACHE / old_name
        if old_name and old_dir != new_dir and old_dir.is_dir() and not new_dir.exists():
            old_dir.rename(new_dir)
            log(f"{app.get('name') or source} 缓存目录改为 {new_dir.name}")


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    legacy = DATA / "config.json"
    if not CONFIG.exists() and legacy.exists():
        save_config(load_json(legacy, DEFAULT))
    elif not CONFIG.exists():
        save_config(DEFAULT)
    migrate_cache_dirs(current_config())
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("网页已开在 8080")
    while True:
        cfg = current_config()
        try:
            with LOCK:
                once()
        except Exception as exc:
            log(f"本轮失败 {exc}")
            notice(current_config(), "电视装包本轮失败", str(exc))
        wait = max(1, int(cfg.get("install_minutes") or 10)) * 60
        time.sleep(wait)


if __name__ == "__main__":
    main()

