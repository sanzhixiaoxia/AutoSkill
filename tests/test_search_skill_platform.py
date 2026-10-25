#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""search_skill_platform.py 的回归测试。

运行（仓库根目录）：
  python -m unittest discover -s tests -v
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "search_skill_platform.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ssp_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EnvFixture(unittest.TestCase):
    """带 QIANFAN_SKILLS 临时目录与模块级补丁的基类。"""

    def setUp(self):
        self.m = load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._saved_env = os.environ.get("QIANFAN_SKILLS")
        os.environ["QIANFAN_SKILLS"] = str(self.root)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._saved_env is None:
            os.environ.pop("QIANFAN_SKILLS", None)
        else:
            os.environ["QIANFAN_SKILLS"] = self._saved_env

    def add_skill(self, tier: str, slug: str, frontmatter: str) -> Path:
        d = self.root / tier / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# {slug}\n", encoding="utf-8")
        return d


class RelevantStrictTests(unittest.TestCase):
    """本地层更严对口判定（_relevant_strict）的边界。"""

    def setUp(self):
        self.m = load_module()

    def test_weak_overlap_rejected(self):
        # 只共享“视频”一个词：查总结命中剪辑工具，必须挡下
        self.assertFalse(self.m._relevant_strict(
            "视频总结", "视频剪辑大师", "专业视频剪辑、加字幕、配乐"))

    def test_lenient_judge_unchanged_for_remote(self):
        # 远端层仍用宽松判定，行为不变
        self.assertTrue(self.m._relevant(
            "视频总结", "视频剪辑大师", "专业视频剪辑、加字幕、配乐"))

    def test_single_core_token_kept(self):
        # 单核心词检索（如“简历”）只有一个可重合词，单词重合即命中
        self.assertTrue(self.m._relevant_strict("简历", "简历模板库", "提供简历模板"))

    def test_multi_token_overlap_kept(self):
        # “视频”“总结”两个词都重合：真对口，保留
        self.assertTrue(self.m._relevant_strict(
            "视频总结", "视频摘要助手", "输入视频链接，输出视频总结文字稿"))

    def test_substring_kept(self):
        self.assertTrue(self.m._relevant_strict("会议纪要", "meeting", "整理会议记录，生成会议纪要"))

    def test_verb_plus_core_may_miss(self):
        # 已知代价：动词+核心词的检索词可能匹配不上本地条目，由远端层补足
        self.assertFalse(self.m._relevant_strict("做份简历", "简历模板库", "提供简历模板"))


class JunkTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def test_junk_names(self):
        for name, slug in [("123", "123"), ("test", "test"), ("ab", "ab"), ("!!!", "sym")]:
            self.assertTrue(self.m.is_junk(name, slug, "描述"), name)

    def test_normal_name_kept(self):
        self.assertFalse(self.m.is_junk("简历助手", "resume-helper", "写简历"))


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, text: str) -> dict:
        p = Path(self._tmp.name) / "SKILL.md"
        p.write_text(text, encoding="utf-8")
        return self.m._read_frontmatter(p)

    def test_plain_fields(self):
        fm = self._write("---\nname: demo\ndescription: 一句话描述\n---\n正文")
        self.assertEqual(fm.get("name"), "demo")
        self.assertEqual(fm.get("description"), "一句话描述")

    def test_folded_marker_not_leaked(self):
        # 多行 >- 折叠：标记本身不应混进描述值
        fm = self._write("---\nname: demo\ndescription: >-\n  第一行\n  第二行\n---\n")
        self.assertEqual(fm.get("description"), "第一行 第二行")

    def test_no_frontmatter(self):
        fm = self._write("没有 frontmatter 的正文")
        self.assertEqual(fm, {})


class LocalLayerTests(EnvFixture):
    def test_scan_finds_installed_skill(self):
        self.add_skill("user", "meeting-minutes", "name: meeting\n\ndescription: 整理会议记录，生成会议纪要")
        items, errors = self.m.scan_local_layer()
        self.assertEqual([i["slug"] for i in items], ["meeting-minutes"])
        self.assertEqual(errors, {})

    def test_scan_excludes_self(self):
        self_dir = self.add_skill("user", "AutoSkill", "name: AutoSkill\ndescription: 自动加载技能")
        saved = self.m.SKILL_DIR
        self.m.SKILL_DIR = self_dir
        self.addCleanup(setattr, self.m, "SKILL_DIR", saved)
        self.add_skill("user", "other-skill", "name: other\ndescription: 其他技能")
        items, _ = self.m.scan_local_layer()
        self.assertEqual([i["slug"] for i in items], ["other-skill"])

    def test_run_search_local_hit_stops_chain(self):
        self.add_skill("user", "meeting-minutes", "name: meeting\n\ndescription: 整理会议记录，生成会议纪要")
        result = self.m.run_search(["会议纪要"], limit=5, top=5, do_filter=True)
        self.assertEqual(result["active_layer"], self.m.LAYER_LOCAL)
        self.assertEqual(len(result["layers"]), 1)  # 提前返回，未触远端
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["candidates"][0]["slug"], "meeting-minutes")

    def test_run_search_weak_local_falls_through(self):
        # 弱相关本地条目不得挡路：本地判空后继续降级（无检索入口 → 通用兜底）
        self.add_skill("user", "video-cut", "name: 视频剪辑大师\ndescription: 专业视频剪辑、加字幕、配乐")
        result = self.m.run_search(["视频总结"], limit=5, top=5, do_filter=True)
        self.assertEqual(result["layers"][0]["layer"], self.m.LAYER_LOCAL)
        self.assertEqual(result["layers"][0]["status"], "empty")
        self.assertEqual(result["active_layer"], self.m.LAYER_GENERIC)
        self.assertIn("finder", result["errors"])


if __name__ == "__main__":
    unittest.main()
