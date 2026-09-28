#!/usr/bin/env python3
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class LogicTests(unittest.TestCase):
    def test_arm64_prefers_v8(self):
        assets = [
            {"name": "app-universal.apk"},
            {"name": "app-armeabi-v7a.apk"},
            {"name": "app-arm64-v8a.apk"},
        ]
        self.assertEqual(app.pick_asset(assets, "arm64")["name"], "app-arm64-v8a.apk")

    def test_v7_rejects_v8(self):
        assets = [{"name": "app-arm64-v8a.apk"}, {"name": "app-armeabi-v7a.apk"}]
        self.assertEqual(app.pick_asset(assets, "arm")["name"], "app-armeabi-v7a.apk")

    def test_plain_apk_is_allowed(self):
        self.assertEqual(app.pick_asset([{"name": "app.apk"}], "arm64")["name"], "app.apk")

    def test_reasons(self):
        self.assertIn("卸载", app.install_reason("INSTALL_FAILED_UPDATE_INCOMPATIBLE"))
        self.assertIn("版本号", app.install_reason("INSTALL_FAILED_VERSION_DOWNGRADE"))
        self.assertIn("完整", app.install_reason("INSTALL_PARSE_FAILED_NOT_APK"))
        self.assertIn("断开", app.install_reason("device offline"))

    def test_both_arm_widths_are_kept(self):
        assets = [
            {"name": "app-arm64-v8a.apk"},
            {"name": "app-armeabi-v7a.apk"},
            {"name": "app-x86_64.apk"},
        ]
        names = [item["name"] for item in app.arm_assets(assets)]
        self.assertEqual(names, ["app-arm64-v8a.apk", "app-armeabi-v7a.apk"])
        self.assertEqual(app.asset_for_device(assets, "arm64")["name"], "app-arm64-v8a.apk")
        self.assertEqual(app.asset_for_device(assets, "arm")["name"], "app-armeabi-v7a.apk")

    def test_yaml_roundtrip_keeps_devices_and_apps(self):
        data = {
            "devices": [{"name": "电视", "ip": "10.10.0.14", "port": 5555}],
            "apps": [{"name": "示例", "source": "https://github.com/owner/app"}],
            "notify": {"feishu": "", "pushplus": "token", "bark": ""},
            "pull_hours": 24,
            "install_minutes": 30,
        }
        back = app.parse_yaml(app.dump_yaml(data))
        self.assertEqual(back["devices"], data["devices"])
        self.assertEqual(back["apps"], data["apps"])
        self.assertEqual(back["notify"]["pushplus"], "token")
        self.assertEqual(back["install_minutes"], 30)

    def test_prune_keeps_old_when_new_file_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            app.CACHE = Path(tmp)
            folder = app.CACHE / "owner_app"
            folder.mkdir()
            (folder / "v1-app.apk").write_bytes(b"old")
            app.prune_old_apks("owner/app", [{"name": "app.apk"}], "v2", "示例")
            self.assertTrue((folder / "v1-app.apk").exists())
            new = folder / "v2-app.apk"
            new.write_bytes(b"new")
            app.prune_old_apks("owner/app", [{"name": "app.apk"}], "v2", "示例")
            names = sorted(item.name for item in folder.glob("*.apk"))
            self.assertEqual(names, ["v2-app.apk"])

    def test_chinese_direct_link_keeps_filename(self):
        source = "https://github.com/youhunwl/TVAPP/raw/main/电视直播/千寻_1.1.5.apk"
        self.assertEqual(app.cache_key(source), "千寻_1.1.5.apk")


if __name__ == "__main__":
    unittest.main()
