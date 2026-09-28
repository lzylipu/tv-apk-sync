#!/usr/bin/env python3
"""Watch GitHub releases and install the matching APK onto Android TVs."""

import json
import os
import re
import shutil
import subprocess
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
            fields = ("name", "ip", "port") if key == "devices" else ("name", "source")
            for item in value:
                first = True
                for field in fields:
                    mark = "- " if first else "  "
                    first = False
                    lines.append(f"  {mark}{field}: {yaml_quote(item.get(field, ''))}")
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
            target[key.strip()] = int(value) if key.strip() == "port" and str(value).isdigit() else value
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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def http_json(url, token=""):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url, dest, token=""):
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%")))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/octet-stream"})
    if token and "github" in url:
        req.add_header("Authorization", f"Bearer {token}")
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as handle:
            shutil.copyfileobj(resp, handle)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def adb(*args, timeout=60):
    proc = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def connect(address):
    adb("connect", address, timeout=20)
    code, out = adb("devices", timeout=15)
    online = any(line.startswith(address) and "\tdevice" in line for line in out.splitlines())
    return online, out


def prop(address, name):
    code, out = adb("-s", address, "shell", "getprop", name, timeout=20)
    return out.strip() if code == 0 else ""


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


def sdk_int(value):
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else 0


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


def device_profile(address):
    ok, detail = connect(address)
    if not ok:
        return None, detail or "连不上"
    abi = prop(address, "ro.product.cpu.abi")
    abilist = prop(address, "ro.product.cpu.abilist")
    sdk = sdk_int(prop(address, "ro.build.version.sdk"))
    release = prop(address, "ro.build.version.release")
    model = prop(address, "ro.product.model")
    family = abi_family(abi)
    if not family:
        return None, f"认不出 CPU：{abi or abilist or '空'}"
    return {"abi": abi, "abilist": abilist, "sdk": sdk, "release": release, "model": model, "family": family}, ""


def installed_version(address, package):
    code, out = adb("-s", address, "shell", "dumpsys", "package", package, timeout=30)
    if code != 0 or "Unable to find package" in out:
        return ""
    match = re.search(r"versionName=(\S+)", out)
    return match.group(1) if match else ""


def install_reason(out):
    text = out or ""
    if "UPDATE_INCOMPATIBLE" in text:
        return "签名和电视上已装的不一致。要换包先在电视上卸载，这次没有自动卸载"
    if "VERSION_DOWNGRADE" in text:
        return "新包版本号比电视上已装的低"
    if "OLDER_SDK" in text or "NO_MATCHING_ABIS" in text:
        return "这个包和电视的安卓版本或 CPU 不匹配"
    if "INSTALL_PARSE_FAILED" in text or "NOT_APK" in text:
        return "文件不是完整 APK，下载可能中断"
    if "device offline" in text or "not found" in text:
        return "安装过程中电视断开了"
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


def device_address(device):
    if device.get("address"):
        return device["address"]
    return f"{str(device.get('ip', '')).strip()}:{int(device.get('port') or 5555)}"


def known_address(address):
    return address in {device_address(item) for item in current_config().get("devices") or []}


def app_label(address, package):
    code, out = adb("-s", address, "shell", "dumpsys", "package", package, timeout=20)
    label = re.search(r"application-label(?:-zh)?:'([^']*)'", out)
    version = re.search(r"versionName=(\S+)", out)
    return (label.group(1) if label else ""), (version.group(1) if version else "")


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


def resolve_release(source, family, token):
    line = release_line(source)
    repo = line["repo"] if line else github_repo(source)
    if repo:
        releases = http_json(f"https://api.github.com/repos/{repo}/releases?per_page=30", token)
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
    cfg = current_config()
    state = load_json(STATE, {"apps": {}, "checked_at": ""})
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    lines = []
    changed = False
    devices = [item for item in (cfg.get("devices") or []) if device_address(item).split(":")[0]]
    apps = [item for item in (cfg.get("apps") or []) if source_of(item)]
    if not devices or not apps:
        log("还没有设备和软件")
        return

    profiles = {}
    for device in devices:
        address = device_address(device)
        name = device.get("name", address)
        profile, err = device_profile(address)
        if err:
            log(f"{name} 不在线，跳过")
            continue
        profiles[address] = profile
        device["model"] = profile["model"]
        device["abi"] = profile["abi"]
        device["android"] = profile["release"]
        device["seen_at"] = datetime.now().isoformat(timespec="seconds")
        log(f"{name} {profile['model']} Android {profile['release']} {profile['abi']}")
    save_config(cfg)
    checked = state.get("checked_at") or ""
    due = True
    if checked and not force:
        try:
            age = datetime.now() - datetime.fromisoformat(checked)
            due = age.total_seconds() >= max(1, int(cfg.get("pull_hours") or 24)) * 3600
        except ValueError:
            due = True
    if not due:
        log("还没到查版本的时间，只安装已经下好的包")
    family = next((item["family"] for item in profiles.values() if item.get("family")), "arm")

    for app in apps:
        source = source_of(app)
        label = app.get("name") or source
        min_sdk = int(app.get("min_sdk") or 0)
        chosen = {}
        files = []
        if source.startswith("file:"):
            chosen = {"key": source, "tag": "", "name": source.split(":", 1)[-1]}
        elif due:
            try:
                chosen = resolve_release(source, family, token)
            except urllib.error.HTTPError as exc:
                log(f"{label} 查版本失败 HTTP {exc.code}")
                continue
            except Exception as exc:
                lines.append(f"{label} 查版本失败 {exc}")
                log(lines[-1])
                continue
            if chosen.get("error"):
                lines.append(chosen["error"])
                log(lines[-1])
                continue
            files = chosen.get("assets") or [{"name": chosen["name"], "url": chosen["url"], "size": chosen["size"]}]
            for item in files:
                path = cache_dest(source, chosen["tag"], item["name"])
                if not path.exists() or (item["size"] and path.stat().st_size != item["size"]):
                    log(f"下载 {label} {item['name']}")
                    try:
                        download(item["url"], path, token)
                    except Exception as exc:
                        log(f"{label} 下载失败 {exc}")
                        continue
            prune_old_apks(source, files, chosen["tag"], label)
        for device in devices:
            address = device_address(device)
            profile = profiles.get(address)
            name = device.get("name", address)
            if not profile:
                continue
            if min_sdk and profile["sdk"] and profile["sdk"] < min_sdk:
                lines.append(f"{name} Android {profile['release']} 低于 {label} 要求的 SDK {min_sdk}，跳过")
                log(lines[-1])
                continue
            key = f"{address}|{chosen['key']}" if chosen.get("key") else f"{address}|{source}"
            folder = CACHE if source.startswith("file:") else CACHE / cache_key(source)
            cached = [item for item in folder.glob("*.apk") if item.is_file()] if folder.exists() else []
            if source.startswith("file:"):
                wanted = source.split(":", 1)[-1]
                cached = [item for item in cached if item.name == wanted]
            if chosen.get("tag"):
                cached = [item for item in cached if item.name.startswith(chosen["tag"] + "-")] or cached
            picked = asset_for_device([{"name": item.name, "path": item} for item in cached], profile["family"])
            dest = picked["path"] if picked else None
            if dest is None or not dest.exists():
                continue
            prev = state["apps"].get(key, {})
            ident = apk_identity(dest)
            if ident["package"]:
                app["package"] = ident["package"]
                app["apk_version"] = ident["version"]
                app["apk_label"] = ident["label"]
                save_config(cfg)
            if chosen.get("tag") and prev.get("tag") == chosen["tag"]:
                log(f"{name} {label} 已是 {chosen['tag']}，跳过")
                continue
            installed = installed_version(address, ident["package"]) if ident["package"] else ""
            if installed and ident["version"] and installed == ident["version"]:
                if chosen.get("tag"):
                    state["apps"][key] = {"tag": chosen["tag"], "asset": dest.name, "version": ident["version"], "at": datetime.now().isoformat(timespec="seconds")}
                    changed = True
                    save_json(STATE, state)
                log(f"{name} {label} 电视上已是 {installed}，跳过")
                continue
            code, out = adb("-s", address, "install", "-r", str(dest), timeout=300)
            shown = chosen.get("tag") or dest.name
            if code == 0 and "Success" in out:
                state["apps"][key] = {
                    "tag": shown,
                    "asset": dest.name,
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                changed = True
                lines.append(f"{name} {label} 已装 {shown}")
            else:
                lines.append(f"{name} {label} 安装失败：{install_reason(out)}")
            log(lines[-1])
    if changed:
        save_json(STATE, state)
    failures = [line for line in lines if "失败" in line or "没有适合" in line]
    if failures:
        notice(cfg, "安装没有完成", "\n".join(failures))
    elif lines:
        notice(cfg, "安装包已更新", "\n".join(lines))
    else:
        log("没有需要安装的更新")
    if due:
        state["checked_at"] = datetime.now().isoformat(timespec="seconds")
        save_json(STATE, state)


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
            flag = "-3" if kind == "user" else "-s"
            code, out = adb("-s", address, "shell", "pm", "list", "packages", "-f", flag, timeout=30)
            rows=[]
            for line in out.splitlines():
                if "package:" not in line:
                    continue
                body = line.split("package:", 1)[-1]
                path, pkg = body.rsplit("=", 1)
                label, version = app_label(address, pkg.strip())
                rows.append({"package": pkg.strip(), "version": version, "label": label, "path": path})
            rows.sort(key=lambda item: (item["label"] or item["package"]).lower())
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
            rows=[]
            for line in out.splitlines()[1:]:
                parts=line.split()
                if len(parts) < 4 or not parts[1].isdigit():
                    continue
                rss=int(parts[2]) if parts[2].isdigit() else 0
                rows.append({"user": parts[0], "pid": parts[1], "rss": rss, "name": parts[-1]})
            rows.sort(key=lambda item: item["rss"], reverse=True)
            self.send(200, json.dumps(rows, ensure_ascii=False))
        elif self.path == "/api/devices":
            rows = []
            for device in current_config().get("devices") or []:
                address = device_address(device)
                if not address.split(":")[0]:
                    continue
                profile, err = device_profile(address)
                row = {"name": device.get("name") or address, "ip": device.get("ip", ""), "address": address}
                if profile:
                    row.update({"online": True, "model": profile["model"], "android": profile["release"], "abi": profile["abi"]})
                else:
                    row.update({"online": False, "detail": "没开机或 5555 没开"})
                rows.append(row)
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
        else:
            self.send(404, "{}")

    def do_POST(self):
        try:
            if self.path == "/api/upload":
                added, error = save_uploads(self)
                self.send(200, json.dumps({"added": added, "error": error}, ensure_ascii=False))
                return
            if self.path == "/api/config":
                incoming = self.read_json()
                incoming.pop("github_token", None)
                for device in incoming.get("devices") or []:
                    device["port"] = int(device.get("port") or 5555)
                    device["address"] = device_address(device)
                incoming["pull_hours"] = max(1, int(incoming.get("pull_hours") or 24))
                incoming["install_minutes"] = max(1, int(incoming.get("install_minutes") or 10))
                save_config(incoming)
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
                self.send(200, out or f"退出码 {code}", "text/plain; charset=utf-8")
            elif self.path == "/api/app-action":
                body = self.read_json()
                address = body.get("address", "")
                package = (body.get("package") or "").strip()
                action = body.get("action", "")
                if not known_address(address) or not re.fullmatch(r"[A-Za-z0-9._]+", package):
                    self.send(400, "设备或包名不对", "text/plain; charset=utf-8")
                    return
                commands = {
                    "uninstall": ["pm", "uninstall", "--user", "0", package],
                    "clear": ["pm", "clear", package],
                    "disable": ["pm", "disable-user", "--user", "0", package],
                    "enable": ["pm", "enable", package],
                    "stop": ["am", "force-stop", package],
                }
                if action == "cache":
                    code, out = adb("-s", address, "shell", "pm", "clear", "--cache-only", package, timeout=30)
                    text = (out or "").strip()
                    if code == 0 and "Success" in text:
                        out = "缓存已清"
                    else:
                        code, out = adb("-s", address, "shell", "cmd", "package", "trim-caches", "999999999999", timeout=30)
                        out = "已请求系统清理缓存" if code == 0 else (text or out)
                elif action in commands:
                    code, out = adb("-s", address, "shell", *commands[action], timeout=40)
                else:
                    self.send(400, "不支持这个操作", "text/plain; charset=utf-8")
                    return
                self.send(200, (out or "完成").strip(), "text/plain; charset=utf-8")
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
                    self.send(200, "已删除", "text/plain; charset=utf-8")
                    return
                if action == "push":
                    address = body.get("address") or ""
                    if not known_address(address):
                        self.send(400, "先选一台已保存的设备", "text/plain; charset=utf-8")
                        return
                    code, out = adb("-s", address, "install", "-r", str(path), timeout=300)
                    if code == 0 and "Success" in out:
                        self.send(200, "已推送到设备", "text/plain; charset=utf-8")
                    else:
                        self.send(200, install_reason(out), "text/plain; charset=utf-8")
                    return
                self.send(400, "不支持这个操作", "text/plain; charset=utf-8")
            elif self.path == "/api/pull":
                with LOCK:
                    once(force=True)
                self.send(200, "已检查一轮，结果在最近日志里", "text/plain; charset=utf-8")
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

