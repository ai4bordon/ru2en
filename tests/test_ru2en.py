import json
import importlib
import sys
import tempfile
import unittest
from pathlib import Path


class Ru2EnConfigTests(unittest.TestCase):
    def setUp(self):
        # Reload module to ensure globals are fresh for each test
        if 'ru2en' in sys.modules:
            importlib.reload(sys.modules['ru2en'])
        self.module = importlib.import_module('ru2en')

    def test_load_cfg_returns_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "ru2en.json"
            original_path = self.module.CFG_PATH
            self.module.CFG_PATH = cfg_path
            try:
                cfg = self.module.load_cfg()
            finally:
                self.module.CFG_PATH = original_path
            self.assertEqual(cfg["stt_model"], self.module.DEFAULT_CFG["stt_model"])
            self.assertEqual(cfg["style_profile"], self.module.DEFAULT_CFG["style_profile"])

    def test_save_cfg_writes_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "ru2en.json"
            original_path = self.module.CFG_PATH
            self.module.CFG_PATH = cfg_path
            try:
                cfg = dict(self.module.DEFAULT_CFG)
                cfg["openai_api_key"] = "TESTKEY"
                self.module.save_cfg(cfg)
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            finally:
                self.module.CFG_PATH = original_path
            self.assertEqual(saved["openai_api_key"], "TESTKEY")


class Ru2EnTextTests(unittest.TestCase):
    def setUp(self):
        if 'ru2en' in sys.modules:
            importlib.reload(sys.modules['ru2en'])
        self.module = importlib.import_module('ru2en')

    def test_looks_like_russian_true_for_cyrillic(self):
        self.assertTrue(self.module.looks_like_russian("Привет мир"))

    def test_looks_like_russian_false_for_latin(self):
        self.assertFalse(self.module.looks_like_russian("Hello world"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
