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


if __name__ == "__main__":
    unittest.main()
