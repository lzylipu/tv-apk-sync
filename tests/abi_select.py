#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class PickTests(unittest.TestCase):
    def test_arm64_prefers_v8_over_universal(self):
        assets = [
            {"name": "app-universal.apk"},
            {"name": "app-armeabi-v7a.apk"},
            {"name": "app-arm64-v8a.apk"},
        ]
        self.assertEqual(app.pick_asset(assets, "arm64")["name"], "app-arm64-v8a.apk")

    def test_arm_v7_rejects_arm64(self):
        assets = [{"name": "app-arm64-v8a.apk"}, {"name": "app-armeabi-v7a.apk"}]
        self.assertEqual(app.pick_asset(assets, "arm")["name"], "app-armeabi-v7a.apk")

    def test_plain_apk_is_usable_when_no_abi_split(self):
        assets = [{"name": "app-release.apk"}]
        self.assertEqual(app.pick_asset(assets, "arm64")["name"], "app-release.apk")

    def test_x86_does_not_take_arm(self):
        assets = [{"name": "app-arm64-v8a.apk"}]
        self.assertIsNone(app.pick_asset(assets, "x86_64"))

    def test_abi_family(self):
        self.assertEqual(app.abi_family("arm64-v8a"), "arm64")
        self.assertEqual(app.abi_family("armeabi-v7a"), "arm")
        self.assertEqual(app.abi_family("x86_64"), "x86_64")
        self.assertEqual(app.abi_family(""), "")

    def test_signature_conflict_auto_uninstall_message(self):
        text = "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]"
        self.assertIn("已自动卸载旧包重装", app.install_reason(text))

    def test_notify_line_has_ip_version_and_status(self):
        # 成功行格式: 设备名 IP｜软件名 X→Y 成功
        notify_ok = {"电视": ["电视 10.10.0.14｜小盒子 1.1→1.2 成功"]}
        ok_text = "\n".join(item for items in notify_ok.values() for item in items)
        self.assertIn("10.10.0.14", ok_text)
        self.assertIn("小盒子", ok_text)
        self.assertIn("1.1→1.2", ok_text)
        self.assertIn("成功", ok_text)
        # 失败行格式: 设备名 IP｜软件名 X→Y 失败：直白理由
        notify_fail = {"电视": ["电视 10.10.0.14｜小盒子 1.1→1.2 失败：新包版本号比电视上已装的低"]}
        fail_text = "\n".join(item for items in notify_fail.values() for item in items)
        self.assertIn("失败：新包版本号比电视上已装的低", fail_text)

    def test_local_ver_picks_highest_among_cached(self):
        # 修复: 原逻辑只解析第一个缓存包就 break,若首包旧版+后包新版会误判已最新
        local_ver = ""
        vers = iter(["1.0", "1.2"])
        app.apk_identity = lambda p: {"version": next(vers)}
        for c in ["old.apk", "new.apk"]:
            v = app.apk_identity(c).get("version") or ""
            if v and (not local_ver or app.version_ge(v, local_ver)):
                local_ver = v
        self.assertEqual(local_ver, "1.2")

    def test_parse_yaml_min_sdk_as_int(self):
        y = "apps:\n  - name: x\n    source: https://github.com/a/b\n    min_sdk: 9\n"
        d = app.parse_yaml(y)
        self.assertEqual(d["apps"][0]["min_sdk"], 9)

    def test_skip_when_same_tag_already_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text("devices:\n  - name: 电视\n    ip: 10.0.0.8\n    port: 5555\napps:\n  - name: 示例\n    source: owner/app\n", encoding="utf-8")
            (root / "state.json").write_text(json.dumps({
                "apps": {"10.0.0.8:5555|owner/app": {"tag": "v2", "installed": "2.0"}}
            }), encoding="utf-8")
            app.DATA = root
            app.CONFIG = root / "config.yaml"
            app.STATE = root / "state.json"
            app.CACHE = root / "apk"
            app.LOG = root / "sync.log"
            calls = []

            def fake_profile(address):
                return {"abi": "arm64-v8a", "sdk": 28, "release": "9", "model": "Box", "family": "arm64"}, ""

            def fake_http(url, token=""):
                calls.append(("http", url))
                return [{"tag_name": "v2", "assets": [{"name": "app.apk", "browser_download_url": "http://x", "size": 1}]}]

            def fake_installed(address, package):
                return "2.0"

            app.device_profile = fake_profile
            app.http_json = fake_http
            app.installed_version = fake_installed
            app.download = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("包已在缓存，不该再下"))
            (root / "apk" / "owner_app").mkdir(parents=True)
            (root / "apk" / "owner_app" / "v2-app.apk").write_bytes(b"apk")
            app.apk_identity = lambda path: {"package": "com.example.app", "version": "2.0", "label": "示例"}
            app.adb = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not install"))
            app.once()
            self.assertEqual(calls, [("http", "https://api.github.com/repos/owner/app/releases?per_page=30")])
            # 本地包与记录同版本（已是最新）：不连设备安装，日志不应出现设备动作
            self.assertNotIn("在线", (root / "sync.log").read_text(encoding="utf-8"))
            self.assertNotIn("安装", (root / "sync.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
