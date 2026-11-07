#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_prd.py 冒烟测试。

只测「确定性提取」这一层：能不能跑、产物在不在、图文配对有没有断。
不测「判断得对不对」——那属于模型，不属于脚本。

    python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "extract_prd.py")

# 1x1 PNG，够用来验证「图片被抽出来」这件事
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SAMPLE = """# 客户列表页

销售每天要在这张表里找上周签的客户，现在只能一页页翻。

![客户列表页](fig.png)

图 1 客户列表页，12 列平铺，没有筛选

## 业务规则

- 可搜索字段必须包含列表展示的所有关键列
"""


class TestExtractPrd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm-ronchi-test-")
        self.src = os.path.join(self.tmp, "sample.md")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write(SAMPLE)
        with open(os.path.join(self.tmp, "fig.png"), "wb") as f:
            f.write(PNG_1X1)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT] + list(args),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def test_help_exits_zero(self):
        p = self.run_cli("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("extract_prd", p.stdout)

    def test_missing_target_is_rejected(self):
        p = self.run_cli(os.path.join(self.tmp, "does-not-exist.md"))
        self.assertNotEqual(p.returncode, 0, "目标不存在时必须非 0 退出")

    def test_md_extraction_produces_artifacts(self):
        outdir = os.path.join(self.tmp, "out")
        p = self.run_cli(self.src, "-o", outdir)
        self.assertEqual(p.returncode, 0, p.stderr)

        for name in ("content.md", "figure-text-map.json", "manifest.json"):
            self.assertTrue(os.path.exists(os.path.join(outdir, name)), "缺产物：" + name)

        with open(os.path.join(outdir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["files"][0]["figures"], 1, "图没被抽出来")

        figures_dir = os.path.join(outdir, "figures")
        self.assertTrue(os.path.isdir(figures_dir))
        self.assertEqual(len(os.listdir(figures_dir)), 1)

    def test_figure_text_map_keeps_pairing(self):
        outdir = os.path.join(self.tmp, "out2")
        p = self.run_cli(self.src, "-o", outdir)
        self.assertEqual(p.returncode, 0, p.stderr)

        with open(os.path.join(outdir, "figure-text-map.json"), encoding="utf-8") as f:
            data = json.load(f)
        figures = data.get("figures", []) if isinstance(data, dict) else data
        self.assertTrue(figures, "图文配对表是空的——这张图旁边明明有说明文字")
        first = figures[0]
        self.assertIn("客户列表页", first.get("nearest_text", ""), "没认出图注")
        self.assertIn(first.get("confidence"), ("high", "medium", "low"))


if __name__ == "__main__":
    unittest.main()
