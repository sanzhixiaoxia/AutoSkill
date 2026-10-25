#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""search_skill_platform.py 的回归测试。

运行（仓库根目录）：
  python -m unittest discover -s tests -v
"""

import contextlib
import hashlib
import importlib.util
import io
import json
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

    def test_shared_single_char_not_enough(self):
        # 只共享单个汉字（如“机”“程”）不算相关——真实宿主长描述的误命中场景
        self.assertFalse(self.m._relevant_strict(
            "订机票", "高考助手", "覆盖升学路径规划、招生计划等场景，机会与风险并存"))
        self.assertFalse(self.m._relevant_strict(
            "行程规划", "可视化", "支持流程、状态、对比、日程等内容渲染"))

    def test_single_bigram_overlap_not_enough(self):
        # 多词检索词只重合一个二字组（如“规划”）时不算对口
        self.assertFalse(self.m._relevant_strict(
            "行程规划", "可视化", "支持流程、状态、对比、规划、日程等内容渲染"))

    def test_two_birgram_overlap_kept(self):
        self.assertTrue(self.m._relevant_strict(
            "文字转语音", "语音合成", "支持文本转语音，发音人与语速可调"))


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

    def test_scan_supports_flat_layout(self):
        # 扁平布局 skills/<skill>（宿主 global/data/skills 实际如此）
        d = self.root / "tts-flat"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\nname: tts\ndescription: 文字转语音配音\n---\n", encoding="utf-8")
        items, errors = self.m.scan_local_layer()
        self.assertIn("tts-flat", [i["slug"] for i in items])
        self.assertEqual(errors, {})

    def test_locate_finder_flat_layout(self):
        finder = self.root / "extension-finder" / "scripts"
        finder.mkdir(parents=True, exist_ok=True)
        (finder / "extension_finder.py").write_text("# finder\n", encoding="utf-8")
        found = self.m.locate_finder()
        self.assertIsNotNone(found)
        self.assertTrue(str(found).endswith("extension_finder.py"))

    def test_run_search_local_hit_stops_chain(self):
        self.add_skill("user", "meeting-minutes", "name: meeting\n\ndescription: 整理会议记录，生成会议纪要")
        result = self.m.run_search(["会议纪要"], limit=5, top=5, do_filter=True)
        self.assertEqual(result["active_layer"], self.m.LAYER_LOCAL)
        self.assertEqual(len(result["layers"]), 1)  # 提前返回，未触远端
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["candidates"][0]["slug"], "meeting-minutes")

    def test_run_search_weak_local_falls_through(self):
        # 弱相关本地条目不得挡路：本地判空后继续降级（打桩 SkillHub 层，避免测试触网）
        self.m.collect_skillhub_layer = lambda queries, *, limit: ([], {"skillhub:x": "stub"})
        self.add_skill("user", "video-cut", "name: 视频剪辑大师\ndescription: 专业视频剪辑、加字幕、配乐")
        result = self.m.run_search(["视频总结"], limit=5, top=5, do_filter=True)
        self.assertEqual(result["layers"][0]["layer"], self.m.LAYER_LOCAL)
        self.assertEqual(result["layers"][0]["status"], "empty")
        self.assertEqual(result["active_layer"], self.m.LAYER_GENERIC)
        self.assertIn("finder", result["errors"])

    def test_local_near_miss_reported(self):
        # "自动加载技能"里"自动/技能"是停用词，本地 autoskill 只重合"加载"被严判定
        # 拦下，但应作为 near_miss 返回，供模型按语义采用（不丢信息）。
        d = self.root / "autoskill"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            "---\nname: AutoSkill\ndescription: 自动分析对话并加载执行所需技能，本地已装优先\n---\n", encoding="utf-8")
        self.m.collect_skillhub_layer = lambda queries, *, limit: ([], {})
        result = self.m.run_search(["自动加载技能"], limit=5, top=5, do_filter=True)
        self.assertEqual(result["layers"][0]["status"], "empty")
        self.assertEqual(result["near_miss"][0]["slug"], "autoskill")
        self.assertIn("加载", result["near_miss"][0]["description"])


class CliFixture(unittest.TestCase):
    """通过 main() 跑 install/enable 子命令并捕获 JSON 输出。"""

    def setUp(self):
        self.m = load_module()
        self._saved_env = os.environ.get("SKILL_INSTALL_ALLOWED_HOSTS")
        self.addCleanup(self._restore_env)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        saved_ledger = self.m.LEDGER_PATH
        self.m.LEDGER_PATH = Path(self._tmp.name) / "ledger.json"
        self.addCleanup(setattr, self.m, "LEDGER_PATH", saved_ledger)

    def _restore_env(self):
        if self._saved_env is None:
            os.environ.pop("SKILL_INSTALL_ALLOWED_HOSTS", None)
        else:
            os.environ["SKILL_INSTALL_ALLOWED_HOSTS"] = self._saved_env

    def cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.m.main(argv)
        js = None
        for line in buf.getvalue().splitlines():
            if line.startswith("{"):
                js = json.loads(line)
        return rc, js


class InstallPolicyTests(CliFixture):
    BASE = ["install", "--slug", "demo", "--source", "hub",
            "--download-url", "https://d.bcebos.com/demo.zip", "--version", "1.0"]

    def test_preview_by_default(self):
        rc, js = self.cli(self.BASE + ["--allow-no-hash"])
        self.assertEqual(rc, 0)
        self.assertFalse(js["executed"])
        self.assertTrue(js["needs_confirmation"])
        self.assertEqual(js["confirm"]["slug"], "demo")
        self.assertEqual(js["confirm"]["version"], "1.0")
        self.assertEqual(js["confirm"]["download_url"], self.BASE[6])

    def test_unknown_host_blocked(self):
        argv = [x if x != self.BASE[6] else "https://evil.example.com/demo.zip" for x in self.BASE]
        rc, js = self.cli(argv + ["--allow-no-hash"])
        self.assertEqual(rc, 2)
        self.assertEqual(js["error_type"], "policy")
        self.assertIn("白名单", js["error"])

    def test_plain_http_blocked(self):
        argv = [x if x != self.BASE[6] else "http://d.bcebos.com/demo.zip" for x in self.BASE]
        rc, js = self.cli(argv + ["--allow-no-hash"])
        self.assertEqual(rc, 2)
        self.assertEqual(js["error_type"], "policy")

    def test_unpinned_version_blocked_unless_acknowledged(self):
        argv = [a for a in self.BASE if a != "--version" and a != "1.0"]
        rc, js = self.cli(argv + ["--allow-no-hash"])
        self.assertEqual(rc, 2)
        self.assertIn("版本未固定", js["error"])
        rc, js = self.cli(argv + ["--allow-no-hash", "--allow-unpinned"])
        self.assertEqual(rc, 0)
        self.assertFalse(js["executed"])

    def test_missing_hash_needs_acknowledgement(self):
        rc, js = self.cli(self.BASE)
        self.assertEqual(rc, 2)
        self.assertIn("哈希", js["error"])
        rc, js = self.cli(self.BASE + ["--allow-no-hash"])
        self.assertEqual(rc, 0)

    def test_env_whitelist_override(self):
        os.environ["SKILL_INSTALL_ALLOWED_HOSTS"] = "internal.example"
        argv = [x if x != self.BASE[6] else "https://mirror.internal.example/demo.zip" for x in self.BASE]
        rc, js = self.cli(argv + ["--allow-no-hash"])
        self.assertEqual(rc, 0)
        # 覆盖后默认域不再放行
        rc, js = self.cli(self.BASE + ["--allow-no-hash"])
        self.assertEqual(rc, 2)

    def test_force_flagged_in_preview(self):
        rc, js = self.cli(self.BASE + ["--allow-no-hash", "--force"])
        self.assertEqual(rc, 0)
        self.assertTrue(js["confirm"]["force"])
        self.assertIn("--yes", js["confirm"]["force_warning"])


class EnableGuardTests(CliFixture):
    def test_enable_preview_by_default(self):
        rc, js = self.cli(["enable", "--slug", "demo"])
        self.assertEqual(rc, 0)
        self.assertFalse(js["executed"])
        self.assertFalse(js["confirm"]["managed_by_this_flow"])

    def test_enable_guard_blocks_unmanaged_without_user_confirmation(self):
        rc, js = self.cli(["enable", "--slug", "demo", "--yes"])
        self.assertEqual(rc, 2)
        self.assertEqual(js["error_type"], "enable_guard")

    def test_enable_guard_passes_with_user_confirmation(self):
        # 守卫通过后走到 finder 缺失（本测试环境无 extension-finder）
        rc, js = self.cli(["enable", "--slug", "demo", "--yes", "--user-confirmed"])
        self.assertEqual(rc, 1)
        self.assertEqual(js["error_type"], "finder_missing")

    def test_enable_managed_needs_no_extra_confirmation(self):
        self.m._record_install("demo", {"kind": "skill"}, self.m.LEDGER_PATH)
        rc, js = self.cli(["enable", "--slug", "demo", "--yes"])
        self.assertEqual(rc, 1)
        self.assertEqual(js["error_type"], "finder_missing")


class HashAndLedgerTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_fetch_and_verify_file_ok(self):
        p = self.dir / "artifact.bin"
        payload = b"hello autoskill"
        p.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        out = self.m._fetch_and_verify(p.as_uri(), digest, self.dir / "v")
        self.assertEqual(out.read_bytes(), payload)

    def test_fetch_and_verify_mismatch(self):
        p = self.dir / "artifact.bin"
        p.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            self.m._fetch_and_verify(p.as_uri(), "0" * 64, self.dir / "v")

    def test_ledger_roundtrip(self):
        path = self.dir / "ledger.json"
        self.assertEqual(self.m._load_ledger(path), {})
        self.m._record_install("demo", {"kind": "skill", "version": "1.0"}, path)
        self.assertEqual(self.m._load_ledger(path)["demo"]["version"], "1.0")


class InstallHintTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def test_hint_includes_version(self):
        item = self.m.norm_remote(
            {"slug": "demo", "displayName": "Demo", "description": "d",
             "version": "1.2.3", "downloadURL": "https://d.bcebos.com/demo.zip"},
            source="skill_platform", kind="skill")
        self.assertIn("--version 1.2.3", item["install_hint"])
        self.assertIn("--download-url https://d.bcebos.com/demo.zip", item["install_hint"])


class SkillHubLayerTests(EnvFixture):
    def test_norm_skillhub(self):
        item = self.m.norm_skillhub({
            "slug": "travel-max", "name": "旅行攻略大师", "description_zh": "行程规划",
            "ownerName": "user_abc", "version": "1.0.0", "downloads": 42})
        self.assertEqual(item["source"], "skillhub")
        self.assertEqual(item["description"], "行程规划")
        self.assertIn("--namespace user_abc", item["install_hint"])
        self.assertIn("--source skillhub", item["install_hint"])

    def test_chain_falls_to_skillhub_when_finder_missing(self):
        # 本地无匹配 + 宿主入口缺失 → SkillHub 层接住（打桩避免真实网络）
        fixture = [{"slug": "travel-max", "name": "旅行攻略大师",
                    "description": "行程规划 旅行攻略", "source": "skillhub", "kind": "skill"}]
        self.m.collect_skillhub_layer = lambda queries, *, limit: (fixture, {})
        result = self.m.run_search(["行程规划"], limit=5, top=5, do_filter=True)
        self.assertEqual(result["active_layer"], self.m.LAYER_SKILLHUB)
        self.assertEqual(result["fallback_level"], 4)
        self.assertEqual(result["layers"][0]["status"], "empty")  # 本地判空
        self.assertEqual(result["candidates"][0]["slug"], "travel-max")

    def test_skillhub_error_falls_to_generic(self):
        self.m.collect_skillhub_layer = lambda queries, *, limit: ([], {"skillhub:x": "boom"})
        result = self.m.run_search(["量子计算"], limit=5, top=5, do_filter=True)
        self.assertEqual(result["active_layer"], self.m.LAYER_GENERIC)
        self.assertEqual(result["fallback_level"], 5)


class SkillHubInstallTests(CliFixture):
    def test_preview_by_default(self):
        rc, js = self.cli(["install", "--slug", "travel-max", "--source", "skillhub",
                           "--namespace", "user_abc", "--dir", "D:/tmp-skills"])
        self.assertEqual(rc, 0)
        self.assertFalse(js["executed"])
        self.assertEqual(js["confirm"]["source"], "skillhub")
        self.assertEqual(js["confirm"]["namespace"], "user_abc")
        self.assertTrue(js["command"][1:] == ["install", "travel-max", "--namespace", "user_abc",
                                              "--dir", "D:/tmp-skills"] or
                        js["command"][0].endswith("skillhub"))

    def test_no_version_pin_required_for_skillhub(self):
        rc, js = self.cli(["install", "--slug", "travel-max", "--source", "skillhub"])
        self.assertEqual(rc, 0)  # 无 --version / 哈希要求，仅展示确认

    def test_cli_missing_when_confirmed(self):
        saved = self.m.shutil.which
        self.m.shutil.which = lambda name: None
        self.addCleanup(setattr, self.m.shutil, "which", saved)
        rc, js = self.cli(["install", "--slug", "travel-max", "--source", "skillhub", "--yes"])
        self.assertEqual(rc, 1)
        self.assertEqual(js["error_type"], "cli_missing")


if __name__ == "__main__":
    unittest.main()
