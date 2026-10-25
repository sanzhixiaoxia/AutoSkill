#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动加载技能（AutoSkill）的检索 / 安装入口（分层降级 + 噪声过滤 + 非交互安装）。

分层降级链（本地优先、网上补足；按顺序，命中即停）：
  1. 已装本地  local_installed    —— 本机已安装的 skill / 套件（离线扫描，最快且不依赖网络）
  2. 优质平台  quality_platform   —— skill 技能平台（skill hub）
  3. 其他远端  other_remote       —— 技能广场（square）等远端来源
  4. 开放社区  skillhub           —— SkillHub（api.skillhub.cn，免鉴权；不依赖宿主检索入口）
  5. 通用工具  generic_tools      —— 以上都没有时，交给通用工具兜底

检索默认对候选做**噪声过滤**：剔除名称/标识无信息量（如纯数字、纯符号、占位名）的垃圾条目，
以及只靠零星字符蹭到、并不对口的弱相关条目；过滤数量在 filter 字段如实报告。
需要看原始候选时加 --no-filter 关闭过滤。
本地层（local_installed）另用更严的对口判定：整词命中或 ≥2 个非填充词重合才算相关，
避免弱相关的已装条目挡住更对口的远端能力。

子命令（子进程层面无交互：stdin 关闭、不弹窗；安装/启用在对话层需用户确认，默认只预览）：
  search（默认）  分层检索并按匹配度择优
  install         安装候选：默认只做策略校验 + 预览（域名白名单 / 版本固定 / 哈希校验），
                  返回 confirm（来源、版本、下载地址、将执行的命令）供向用户展示；
                  用户明确同意后加 --yes 才真正执行
  enable          启用已下载但被禁用的候选：默认预览；非本流程安装（台账外）的候选
                  需额外 --user-confirmed，避免覆盖用户此前的禁用决定

用法：
  python3 scripts/search_skill_platform.py \
      --query "整理会议记录" --query "会议纪要" [--limit 5] [--top 5] [--no-filter]
  python3 scripts/search_skill_platform.py install --slug <slug> --source hub \
      --download-url <url> --version <v> [--sha256 <hex>]        # 预览 + 策略校验
  python3 scripts/search_skill_platform.py install --slug <slug> --source hub \
      --download-url <url> --version <v> --yes                   # 用户确认后执行
  python3 scripts/search_skill_platform.py install --type plugin --plugin-code <code> --yes
  python3 scripts/search_skill_platform.py enable --slug <slug> --yes [--user-confirmed]

输出：sentinel 包裹的单行 JSON，便于宿主解析：

  ===SKILL_PLATFORM_CANDIDATES===
  {"query":[...],"active_layer":"...","fallback_level":1,"candidates":[...],...}
  ===END===

退出码：0=脚本跑通（候选可能为空，此时 active_layer 为 generic_tools；安装预览也算跑通）；
1=参数错误或子命令用法错误；2=所有检索层都失败，或安装/启用被策略拦下、失败、超时。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]

# Windows 管道常为 GBK 等本地编码：候选描述含 emoji 等无法编码的字符时降级为替换，而不是崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

# skill 技能平台（hub）的检索索引标识；与平台检索协议一致。
HUB_INDEX = "clawHub"
# 广场（square）检索索引：个人版 / 企业版不同；与宿主一致。
SQUARE_INDEX_PERSONAL = "baiduMate"
SQUARE_INDEX_ENTERPRISE = "enterprise"

# skill 下载地址域名白名单（https，子域后缀匹配）。环境变量 SKILL_INSTALL_ALLOWED_HOSTS
# 可整体替换（逗号分隔）。策略细节见 references/installer-contract.md。
DEFAULT_ALLOWED_HOSTS = (
    "bcebos.com",  # 百度对象存储（skill hub 分发域）
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
)
# 安装台账：记录经本流程安装的候选；enable 据此区分"本流程装的"与"用户可能有意禁用的"。
LEDGER_PATH = Path(__file__).resolve().parent / ".install-ledger.json"

SENTINEL_BEGIN = "===SKILL_PLATFORM_CANDIDATES==="
SENTINEL_END = "===END==="

MAX_QUERIES = 3
DEFAULT_LIMIT = 5
DEFAULT_TOP = 5
DESC_VIEW = 2000

LAYER_QUALITY = "quality_platform"
LAYER_REMOTE = "other_remote"
LAYER_SKILLHUB = "skillhub"
LAYER_LOCAL = "local_installed"
LAYER_GENERIC = "generic_tools"

# SkillHub 开放技能社区：免鉴权 HTTP 检索 + skillhub CLI 安装（契约见 references/installer-contract.md）。
SKILLHUB_API = "https://api.skillhub.cn/api/skills"
SKILLHUB_TIMEOUT = 8

LAYER_LABELS = {
    LAYER_LOCAL: "已装本地能力",
    LAYER_QUALITY: "优质平台（skill 技能平台 / hub）",
    LAYER_REMOTE: "其他远端来源（技能广场等）",
    LAYER_SKILLHUB: "SkillHub 开放社区（免鉴权）",
    LAYER_GENERIC: "通用工具兜底",
}
FALLBACK_LEVEL = {
    LAYER_LOCAL: 1,
    LAYER_QUALITY: 2,
    LAYER_REMOTE: 3,
    LAYER_SKILLHUB: 4,
    LAYER_GENERIC: 5,
}


def log(msg: str, *, level: str = "info") -> None:
    print(f"[skill-router][{level}] {msg}", file=sys.stderr, flush=True)


# ── 定位宿主的能力发现入口 ────────────────────────────────────────────────

def _finder_candidates() -> list[Path]:
    cands: list[Path] = []

    override = os.environ.get("SKILL_PLATFORM_FINDER", "").strip()
    if override:
        cands.append(Path(override))

    rel = Path("extension-finder") / "scripts" / "extension_finder.py"
    roots: list[Path] = []
    env_root = os.environ.get("QIANFAN_SKILLS", "").strip()
    if env_root:
        roots.append(Path(env_root))
    # 技能目录通常位于 <workspace>/skills/<tier>/<skill>，据此回推 skills 根目录。
    for parent in SKILL_DIR.parents:
        if parent.name == "skills":
            roots.append(parent)
            break

    for root in roots:
        for tier in ("official", "user"):
            cands.append(root / tier / rel)
        try:
            cands.extend(sorted(root.glob(f"*/{rel}")))  # skills/<tier>/<skill> 分层布局
            cands.append(root / rel)                     # skills/<skill> 扁平布局（宿主 global/data/skills 实际如此）
        except OSError:
            pass
    return cands


def locate_finder() -> Path | None:
    seen: set[str] = set()
    for path in _finder_candidates():
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    return None


def load_finder(path: Path):
    """按文件路径加载能力发现入口模块；失败即抛出，交由调用方报告。"""
    spec = importlib.util.spec_from_file_location("extension_finder", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载检索入口模块: {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclass 等依赖 sys.modules 中的模块登记，必须先登记再执行。
    sys.modules["extension_finder"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("extension_finder", None)
        raise
    return module


# ── 文本匹配与打分（纯本地计算）────────────────────────────────────────────

def _lcs(a: str, b: str) -> int:
    """最长公共子串长度。"""
    if not a or not b:
        return 0
    if a in b:
        return len(a)
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, start=1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _term_score(term: str, text: str) -> float:
    q = (term or "").strip().lower()
    if not q:
        return 0.0
    t = (text or "").lower()
    if not t:
        return 0.0
    if q in t:
        return len(q) * 4.0
    uniq = set(q)
    coverage = len(uniq & set(t)) / len(uniq) if uniq else 0.0
    return _lcs(q, t) * 2.0 + coverage * len(q)


# 只靠这些“填充式”词对建立的相关性不算数：查询词与候选只共享这类泛词时，视为噪声。
STOP_BIGRAMS = {
    "生成", "工具", "助手", "技能", "智能", "自动", "支持", "使用", "相关", "一个", "可以", "进行",
    "帮助", "提供", "服务", "系统", "管理", "内容", "数据", "信息", "平台", "功能", "应用", "各种",
    "多种", "快速", "简单", "便捷", "一键", "高效", "专业", "完整", "全面", "能力", "场景", "任务",
    "实现", "完成", "操作", "配置", "设置", "基于", "通过", "以及", "并且", "同时", "例如", "包括",
    "设计", "制作", "分析", "编辑", "处理", "转换", "美化",
}
STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "tool", "tools", "skill", "skills",
    "service", "services", "api", "sdk", "demo", "example", "test", "util", "utils", "helper",
    "assistant", "generator", "generation", "generate", "gen", "based", "using", "support",
    "system", "platform", "framework", "plugin",
}
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def _term_tokens(term: str) -> tuple[set[str], set[str]]:
    """把检索词拆成汉字二字组与拉丁词，用于判断“是否有实质性的词级重合”。"""
    q = (term or "").strip().lower()
    bigrams = {
        q[i:i + 2]
        for i in range(len(q) - 1)
        if _CJK_RE.match(q[i]) and _CJK_RE.match(q[i + 1])
    }
    words = set(_WORD_RE.findall(q))
    return bigrams, words


def _relevant(term: str, name: str, desc: str) -> bool:
    """判断某个检索词是否与候选“实质相关”。

    相关 = 检索词整体出现在名称/描述中，或与名称/描述存在**非填充词**的词级重合
    （汉字二字组 / 拉丁词，且不在 STOP 词表内）。这样既能保留“机票酒店预订 → W酒店查询与预订”
    这类正常命中，又能剔除只靠“生成”“工具”等泛词蹭分、并不对口的条目。
    """
    q = (term or "").strip().lower()
    if not q:
        return False
    n = (name or "").lower()
    d = (desc or "")[:DESC_VIEW].lower()
    if (n and q in n) or (d and q in d):
        return True
    bigrams, words = _term_tokens(q)
    dist_bigrams = {b for b in bigrams if b not in STOP_BIGRAMS}
    dist_words = {w for w in words if w not in STOP_WORDS}
    for text in (n, d):
        if not text:
            continue
        if any(b in text for b in dist_bigrams):
            return True
        if any(w in text for w in dist_words):
            return True
    if not bigrams and not words:
        # 极短 / 纯符号词：退回字符包含判断
        return any(ch in n + d for ch in q)
    return False


def _distinct_tokens(term: str) -> set[str]:
    """检索词自身的非填充词集合（汉字二字组 + 拉丁词，剔除 STOP 词表）。"""
    bigrams, words = _term_tokens(term)
    return {b for b in bigrams if b not in STOP_BIGRAMS} | {w for w in words if w not in STOP_WORDS}


def _distinct_overlap(term: str, name: str, desc: str) -> set[str]:
    """检索词与名称/描述实际重合的非填充词集合。"""
    n = (name or "").lower()
    d = (desc or "")[:DESC_VIEW].lower()
    tokens = _distinct_tokens(term)
    hits: set[str] = set()
    for text in (n, d):
        if text:
            hits |= {t for t in tokens if t in text}
    return hits


def _relevant_strict(term: str, name: str, desc: str) -> bool:
    """本地层更严的对口判定。

    本地优先后，弱相关的已装条目会挡住更对口的远端能力（例：查“视频总结”命中本地的
    “视频剪辑”）。本地层因此要求更实的证据：整词出现在名称/描述中，或 ≥2 个非填充词
    重合；检索词本身只有一个非填充词（如“简历”“地图”）时，单词重合也算。
    代价是“做份简历”这类动词+核心词的检索词可能匹配不上，此时落到远端寻找即可。
    """
    q = (term or "").strip().lower()
    if not q:
        return False
    n = (name or "").lower()
    d = (desc or "")[:DESC_VIEW].lower()
    if (n and q in n) or (d and q in d):
        return True
    tokens = _distinct_tokens(q)
    if not tokens:
        # 极短 / 纯符号词（无可拆词）：退回字符包含判断（与宽松判定一致）
        return any(ch in n + d for ch in q)
    hits = _distinct_overlap(q, n, d)
    if not hits:
        return False
    return len(hits) >= 2 or len(tokens) <= 1


def score_candidate(terms: list[str], name: str, description: str) -> tuple[float, list[str]]:
    name = name or ""
    description = description or ""
    desc_view = description[:DESC_VIEW]
    total = 0.0
    matched: list[str] = []
    for term in terms:
        s = _term_score(term, name) * 1.5 + _term_score(term, desc_view)
        if s > 0:
            matched.append(term)
        total += s
    return round(total, 3), matched


# ── 噪声过滤 ──────────────────────────────────────────────────────────────

# 明显的占位/测试名（大小写无关）。只收录“几乎不可能是有意命名的正经技能”的词。
NAME_BLOCKLIST = {
    "test", "tests", "demo", "tmp", "temp", "untitled", "unnamed", "undefined",
    "null", "none", "n/a", "na", "placeholder", "sample", "foo", "bar", "baz",
    "未命名", "占位", "无标题", "示例", "测试", "xxx", "xxx1", "test1", "aaaa",
}
# 纯数字 / 纯符号 / 不含任何字母或汉字 的名称或标识视为垃圾。
_DIGITS_RE = re.compile(r"^\d+$")
_SYMBOLS_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_HAS_LETTER_OR_CJK_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")


def is_junk(name: str, slug: str, description: str) -> bool:
    """判断候选是否为无信息量的噪声条目。"""
    n = (name or "").strip()
    s = (slug or "").strip()
    if not s:
        return True
    if not n and not (description or "").strip():
        return True
    for token in (n, s):
        if not token:
            continue
        low = token.lower()
        if low in NAME_BLOCKLIST:
            return True
        if _DIGITS_RE.fullmatch(token):
            return True
        if _SYMBOLS_ONLY_RE.fullmatch(token):
            return True
    if not _HAS_LETTER_OR_CJK_RE.search(n):
        return True  # 名称里没有任何字母 / 汉字
    if len(n) <= 2:  # 极短名（如 “ab”“11”），无信息量
        return True
    return False


def filter_and_score(items: list[dict], queries: list[str], *, do_filter: bool,
                     strict: bool = False) -> tuple[list[dict], int, int]:
    """对候选做噪声过滤 + 相关性判定 + 打分，返回 (保留项, 垃圾数, 弱相关数)。

    strict=True 用更严的本地层对口判定（_relevant_strict），见其 docstring。
    """
    kept: list[dict] = []
    dropped_junk = 0
    dropped_irrelevant = 0
    judge = _relevant_strict if strict else _relevant
    for item in items:
        name = item.get("name") or ""
        slug = item.get("slug") or ""
        desc = item.get("description") or ""
        if do_filter and is_junk(name, slug, desc):
            dropped_junk += 1
            continue
        score, matched = score_candidate(queries, name, desc)
        strong = [t for t in queries if judge(t, name, desc)]
        if do_filter and not strong:
            dropped_irrelevant += 1
            continue
        item = dict(item)
        item["score"] = score
        item["matched_terms"] = strong if do_filter else matched
        kept.append(item)
    return kept, dropped_junk, dropped_irrelevant


# ── 候选归一 ──────────────────────────────────────────────────────────────

def norm_remote(item: dict, *, source: str, kind: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    slug = str(item.get("slug") or "").strip()
    if not slug:
        return None
    version = item.get("version")
    download = item.get("downloadURL")
    download = download.strip() if isinstance(download, str) else None
    if kind == "plugin":
        hint = f"install --type plugin --plugin-code {slug}"
    else:
        src = "hub" if source == "skill_platform" else "square"
        hint = f"install --slug {slug} --source {src}"
        if download:
            hint += f" --download-url {download}"
        if version:
            hint += f" --version {version}"
    return {
        "slug": slug,
        "name": str(item.get("displayName") or item.get("name") or slug),
        "description": str(item.get("description") or ""),
        "version": str(version) if version else None,
        "downloadURL": download or None,
        "skillId": item.get("skillId"),
        "versionId": item.get("versionId"),
        "source": source,
        "kind": kind,
        "install_hint": hint,
    }


# ── 远端检索（单个来源 / 单个检索词）──────────────────────────────────────

def _search_one(finder, term: str, index: str, kind: str, limit: int, runtime: str) -> list[dict]:
    raw = finder._post_unified_search(term, index, limit, runtime, kind)
    return [x for x in raw if isinstance(x, dict)]


def collect_remote_layer(finder, queries: list[str], *, runtime: str, sources: list[dict],
                         limit: int) -> tuple[list[dict], dict, int]:
    """并发查询同一层内的多个来源 × 多个检索词，合并去重。

    sources 每项形如 {"source": "skill_platform", "index": "clawHub", "kind": "skill"}。
    返回 (归一候选, errors, 成功请求数)。
    """
    errors: dict[str, str] = {}
    ok_jobs = 0
    jobs: list[tuple[str, str, str, str]] = []  # (source, index, kind, term)
    for src in sources:
        for term in queries:
            jobs.append((src["source"], src["index"], src["kind"], term))

    merged: dict[tuple[str, str], dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as pool:
        futures = {}
        for source, index, kind, term in jobs:
            fut = pool.submit(_search_one, finder, term, index, kind, limit, runtime)
            futures[fut] = (source, kind, term)
        for fut, (source, kind, term) in futures.items():
            key = f"{source}:{term}"
            try:
                items = fut.result()
                ok_jobs += 1
            except Exception as exc:  # noqa: BLE001
                errors[key] = f"{type(exc).__name__}: {exc}"
                continue
            for item in items:
                norm = norm_remote(item, source=source, kind=kind)
                if not norm:
                    continue
                merged.setdefault((kind, norm["slug"]), norm)
    return list(merged.values()), errors, ok_jobs


# ── SkillHub 开放社区（免鉴权 HTTP，不依赖宿主检索入口）──────────────────

def _skillhub_search_one(term: str, limit: int) -> list[dict]:
    url = f"{SKILLHUB_API}?keyword={urllib.parse.quote(term)}&sortBy=score&pageSize={max(1, limit)}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutoSkill/1.0"})
    with urllib.request.urlopen(req, timeout=SKILLHUB_TIMEOUT) as resp:  # noqa: S310 固定 https 官方域
        body = json.loads(resp.read().decode("utf-8"))
    items = ((body.get("data") or {}).get("skills")) or []
    return [x for x in items if isinstance(x, dict)]


def norm_skillhub(item: dict) -> dict | None:
    slug = str(item.get("slug") or "").strip()
    if not slug:
        return None
    ns = str(item.get("ownerName") or "").strip()
    version = item.get("version")
    hint = f"install --slug {slug} --source skillhub"
    if ns:
        hint += f" --namespace {ns}"
    return {
        "slug": slug,
        "name": str(item.get("name") or slug),
        "description": str(item.get("description_zh") or item.get("description") or ""),
        "version": str(version) if version else None,
        "downloadURL": None,
        "skillId": None,
        "versionId": None,
        "source": "skillhub",
        "kind": "skill",
        "owner": ns or None,
        "downloads": item.get("downloads"),
        "install_hint": hint,
    }


def collect_skillhub_layer(queries: list[str], *, limit: int) -> tuple[list[dict], dict]:
    """并发查询 SkillHub（每个检索词一次），合并按 slug 去重。返回 (归一候选, errors)。"""
    errors: dict[str, str] = {}
    merged: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(queries)))) as pool:
        futures = {pool.submit(_skillhub_search_one, t, limit): t for t in queries}
        for fut, term in futures.items():
            try:
                items = fut.result()
            except Exception as exc:  # noqa: BLE001
                errors[f"skillhub:{term}"] = f"{type(exc).__name__}: {exc}"
                continue
            for item in items:
                norm = norm_skillhub(item)
                if norm:
                    merged.setdefault(norm["slug"], norm)
    return list(merged.values()), errors


def _skillhub_install_dir() -> str | None:
    """SkillHub 安装目标目录：环境变量 > 已知技能根 > ~/.zcode/skills。"""
    env = os.environ.get("SKILLHUB_INSTALL_DIR", "").strip()
    if env:
        return env
    for root in _skills_roots():
        if root.is_dir():
            return str(root)
    zcode = Path.home() / ".zcode" / "skills"
    return str(zcode) if zcode.is_dir() else None


# ── 本地已装能力（离线扫描）──────────────────────────────────────────────

def _skills_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("QIANFAN_SKILLS", "").strip()
    if env_root:
        roots.append(Path(env_root))
    for parent in SKILL_DIR.parents:
        if parent.name == "skills":
            roots.append(parent)
            break
    return roots


def _plugins_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("QIANFAN_PLUGINS", "").strip()
    if env_root:
        roots.append(Path(env_root))
    for parent in SKILL_DIR.parents:
        if parent.name == "plugins":
            roots.append(parent)
            break
    return roots


def _read_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    data: dict[str, str] = {}
    key: str | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if line[0] not in " \t" and ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            data[key] = v.strip()
        elif key and line[0] in " \t":
            data[key] = (data.get(key, "") + " " + line.strip()).strip()
    for k, v in list(data.items()):
        v = v.strip()
        for marker in (">-", ">", "|-", "|+", "|"):
            if v.startswith(marker):
                v = v[len(marker):].strip()
                break
        data[k] = v.strip().strip('"').strip("'")
    return data


def _resolve_quiet(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def scan_local_layer() -> tuple[list[dict], dict]:
    """离线扫描本机已安装的 skill 与套件，作为第 1 层候选（本地优先）。"""
    items: list[dict] = []
    errors: dict[str, str] = {}
    seen: set[str] = set()
    self_dir = _resolve_quiet(SKILL_DIR)

    for root in _skills_roots():
        if not root.is_dir():
            continue
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError as exc:
            errors[f"skills:{root}"] = str(exc)
            continue
        # 兼容两种布局：skills/<skill>（扁平，宿主 global/data/skills 实际如此）与
        # skills/<tier>/<skill>（official/user 分层）。子目录自带 SKILL.md 即技能；
        # 否则视为 tier 再向下一层。
        skill_dirs: list[Path] = []
        for child in children:
            if (child / "SKILL.md").is_file():
                skill_dirs.append(child)
            else:
                try:
                    skill_dirs.extend(sorted(p for p in child.iterdir() if p.is_dir()))
                except OSError as exc:
                    errors[f"skills:{child}"] = str(exc)
        for skill_dir in skill_dirs:
            md = skill_dir / "SKILL.md"
            if not md.is_file():
                continue
            if _resolve_quiet(skill_dir) == self_dir:
                continue  # 不把 AutoSkill 自己列为候选
            slug = skill_dir.name
            if slug in seen:
                continue
            seen.add(slug)
            fm = _read_frontmatter(md)
            items.append({
                "slug": slug,
                "name": fm.get("name") or slug,
                "description": fm.get("description") or "",
                "version": fm.get("version"),
                "downloadURL": None,
                "skillId": None,
                "versionId": None,
                "source": "local",
                "kind": "skill",
                "install_hint": f"use 已装技能 {slug}（无需安装）",
            })

    for root in _plugins_roots():
        if not root.is_dir():
            continue
        try:
            manifests = list(root.glob("*/*/.claude-plugin/plugin.json"))
        except OSError as exc:
            errors["plugins"] = str(exc)
            continue
        for manifest in sorted(manifests):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            slug = str(data.get("name") or manifest.parents[1].name).strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            items.append({
                "slug": slug,
                "name": str(data.get("displayName") or slug),
                "description": str(data.get("description") or ""),
                "version": str(data["version"]) if data.get("version") else None,
                "downloadURL": None,
                "skillId": None,
                "versionId": None,
                "source": "local",
                "kind": "plugin",
                "install_hint": f"use 已装套件 {slug}（无需安装）",
            })
    return items, errors


# ── 分层降级主流程 ───────────────────────────────────────────────────────

def _rank(kept: list[dict], top: int) -> list[dict]:
    ranked = sorted(
        kept,
        key=lambda it: (-it.get("score", 0.0), len(it.get("name") or ""), it["slug"]),
    )
    out: list[dict] = []
    for rank, item in enumerate(ranked[:top], start=1):
        item = dict(item)
        item["rank"] = rank
        out.append(item)
    return out


def run_search(queries: list[str], *, limit: int, top: int, do_filter: bool) -> dict:
    result: dict = {
        "query": queries,
        "active_layer": None,
        "fallback_level": None,
        "layers": [],
        "candidates": [],
        "filter": {"raw": 0, "junk_dropped": 0, "irrelevant_dropped": 0, "kept": 0},
        "errors": {},
        "ok": False,
    }

    def record_layer(name: str, *, status: str, items: list[dict], errors: dict,
                     strict: bool = False) -> bool:
        kept, dj, di = filter_and_score(items, queries, do_filter=do_filter, strict=strict)
        result["filter"]["raw"] += len(items)
        result["filter"]["junk_dropped"] += dj
        result["filter"]["irrelevant_dropped"] += di
        result["filter"]["kept"] += len(kept)
        entry = {
            "layer": name,
            "label": LAYER_LABELS[name],
            "status": status,
            "raw": len(items),
            "junk_dropped": dj,
            "irrelevant_dropped": di,
            "kept": len(kept),
            "errors": errors,
        }
        if kept and not result["candidates"]:
            entry["candidates"] = _rank(kept, top)
            result["candidates"] = entry["candidates"]
            result["active_layer"] = name
            result["fallback_level"] = FALLBACK_LEVEL[name]
            entry["status"] = "hit"
        elif not kept and status == "ok":
            entry["status"] = "empty"
        result["layers"].append(entry)
        return bool(kept)

    # 第 1 层：已装本地能力（离线扫描——最快，不依赖网络与检索入口；对口判定比远端严）
    items, errors = scan_local_layer()
    log(f"layer={LAYER_LOCAL} raw={len(items)} errors={len(errors)}")
    if record_layer(LAYER_LOCAL, status="ok", items=items, errors=errors, strict=True):
        return _finalize(result)
    if items:
        # 本地未命中时的"差一点"名单：宽松相关但严判定不过的已装候选。
        # 词法证据弱不等于不对口（如"自动加载技能"里有效词只剩"加载"），
        # 交给模型按语义决断：near_miss 里有明显对口的，直接采用，无需降级远端。
        near = []
        for it in items:
            if is_junk(it.get("name") or "", it.get("slug") or "", it.get("description") or ""):
                continue
            name, desc = it.get("name") or "", it.get("description") or ""
            if any(_relevant(t, name, desc) for t in queries):
                score, _ = score_candidate(queries, name, desc)
                near.append({k: it.get(k) for k in ("slug", "name", "description", "version", "source", "install_hint")}
                            | {"score": score, "reason": "本地已装，词法证据弱（near_miss）"})
        near.sort(key=lambda x: -x["score"])
        if near:
            result["near_miss"] = near[:3]

    # 本地未命中，才加载远端检索入口（本地优先：本地命中时不触网、不加载入口）
    finder_path = locate_finder()
    finder = None
    runtime = None
    finder_err = None
    if finder_path is None:
        finder_err = "未找到技能检索入口（extension_finder 脚本）；远端层不可用"
    else:
        try:
            finder = load_finder(finder_path)
            runtime = finder.detect_runtime()
        except Exception as exc:  # noqa: BLE001
            finder_err = f"检索入口加载失败: {exc}"
    if finder_err:
        result["errors"]["finder"] = finder_err
        log(finder_err, level="warn")
    else:
        result["runtime"] = runtime

    # 第 2 层：优质平台（skill hub）
    if finder is not None:
        items, errors, ok_jobs = collect_remote_layer(
            finder, queries, runtime=runtime,
            sources=[{"source": "skill_platform", "index": HUB_INDEX, "kind": "skill"}],
            limit=limit)
        status = "ok" if ok_jobs > 0 else ("error" if errors else "ok")
        log(f"layer={LAYER_QUALITY} raw={len(items)} errors={len(errors)}")
        if record_layer(LAYER_QUALITY, status=status, items=items, errors=errors):
            return _finalize(result)

    # 第 3 层：其他远端（广场 skill + 广场/插件来源）
    if finder is not None and not result["candidates"]:
        is_ent = False
        try:
            is_ent = bool(finder.is_enterprise())
            edition = finder.detect_edition()
        except Exception:  # noqa: BLE001
            edition = "personal"
        square_index = SQUARE_INDEX_ENTERPRISE if is_ent else SQUARE_INDEX_PERSONAL
        sources = [
            {"source": "skill_square", "index": square_index, "kind": "skill"},
            {"source": "plugin_marketplace", "index": edition, "kind": "plugin"},
        ]
        items, errors, ok_jobs = collect_remote_layer(
            finder, queries, runtime=runtime, sources=sources, limit=limit)
        status = "ok" if ok_jobs > 0 else ("error" if errors else "ok")
        log(f"layer={LAYER_REMOTE} raw={len(items)} errors={len(errors)}")
        if record_layer(LAYER_REMOTE, status=status, items=items, errors=errors):
            return _finalize(result)

    # 第 4 层：SkillHub 开放社区（免鉴权 HTTP，不依赖宿主检索入口——宿主入口缺失时也能触达远端）
    if not result["candidates"]:
        items, errors = collect_skillhub_layer(queries, limit=limit)
        status = "error" if len(errors) >= len(queries) else "ok"
        log(f"layer={LAYER_SKILLHUB} raw={len(items)} errors={len(errors)}")
        if record_layer(LAYER_SKILLHUB, status=status, items=items, errors=errors):
            return _finalize(result)

    # 第 5 层：通用工具兜底（无候选，交由模型用通用工具）
    result["layers"].append({
        "layer": LAYER_GENERIC,
        "label": LAYER_LABELS[LAYER_GENERIC],
        "status": "fallback",
        "raw": 0, "junk_dropped": 0, "irrelevant_dropped": 0, "kept": 0, "errors": {},
    })
    result["active_layer"] = LAYER_GENERIC
    result["fallback_level"] = FALLBACK_LEVEL[LAYER_GENERIC]
    return _finalize(result)


def _finalize(result: dict) -> dict:
    # 只要任意一层给出了可用结果（含 generic_tools 兜底），脚本就算跑通。
    result["ok"] = result["active_layer"] is not None
    # 若各层都没命中（本地与远端皆无），仍然算跑通（进入通用兜底），但保留 errors 供上层判断。
    return result


# ── 安装策略：白名单 / 版本固定 / 哈希校验 / 台账 ─────────────────────────

def _allowed_hosts() -> tuple[str, ...]:
    env = os.environ.get("SKILL_INSTALL_ALLOWED_HOSTS", "").strip()
    if env:
        return tuple(h.strip().lower() for h in env.split(",") if h.strip())
    return DEFAULT_ALLOWED_HOSTS


def _host_allowed(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in _allowed_hosts())


def _load_ledger(path: Path | None = None) -> dict:
    path = path or LEDGER_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ledger(data: dict, path: Path | None = None) -> None:
    path = path or LEDGER_PATH
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"安装台账写入失败: {exc}", level="warn")


def _record_install(key: str, entry: dict, path: Path | None = None) -> None:
    data = _load_ledger(path)
    data[key] = entry
    _save_ledger(data, path)


def _fetch_and_verify(url: str, sha256: str, dest_dir: Path) -> Path:
    """下载（https）或读取（file://）工件并校验 sha256，返回本地文件路径；校验失败抛 ValueError。"""
    u = urllib.parse.urlparse(url)
    if u.scheme == "file":
        src = Path(urllib.request.url2pathname(u.path))
        data = src.read_bytes()
    elif u.scheme == "https":
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 调用前已过域名白名单
            data = resp.read()
    else:
        raise ValueError(f"仅支持 https:// 或 file:// 地址，当前协议: {u.scheme or '无'}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != (sha256 or "").strip().lower():
        raise ValueError(f"sha256 校验失败: 期望 {sha256}，实际 {digest}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "verified-download.bin"
    dest.write_bytes(data)
    return dest


def _install_policy(args: argparse.Namespace) -> tuple[dict, list[str]]:
    """安装前策略校验。返回 (供用户确认的展示信息, 阻断性问题列表)。"""
    problems: list[str] = []
    slug = (args.plugin_code or args.slug) if args.kind == "plugin" else args.slug
    confirm: dict = {
        "kind": args.kind,
        "slug": slug,
        "source": getattr(args, "source", None),
        "version": args.version or args.version_id or None,
        "download_url": args.download_url,
        "sha256": args.sha256 or None,
        "force": bool(args.force),
    }
    if args.kind == "skill" and getattr(args, "source", None) == "skillhub":
        confirm["host"] = "SkillHub CLI 解析"
        confirm["namespace"] = (getattr(args, "namespace", None) or None)
        # 下载与完整性由 skillhub CLI 管理（官方源），不走 finder 的 URL/版本/哈希强制项
    elif args.kind == "skill":
        url = args.download_url
        if url:
            u = urllib.parse.urlparse(url)
            host = (u.hostname or "").lower()
            if u.scheme == "https":
                confirm["host"] = host or None
                if not _host_allowed(host):
                    problems.append(
                        f"下载域名不在白名单: {host or '无'}（可用环境变量 SKILL_INSTALL_ALLOWED_HOSTS 配置）")
            elif u.scheme == "file":
                confirm["host"] = "本地文件"
            else:
                problems.append(f"下载地址必须为 https://（本地文件用 file://），当前协议: {u.scheme or '无'}")
        else:
            confirm["host"] = "由安装器解析"
        if not (args.version or args.version_id or args.skill_id) and not args.allow_unpinned:
            problems.append("版本未固定：请传 --version（或 --skill-id/--version-id）；用户知情同意不固定时加 --allow-unpinned")
        if not args.sha256 and not args.allow_no_hash:
            problems.append("未做哈希校验：平台提供哈希时传 --sha256；平台无哈希且用户知情时加 --allow-no-hash")
    else:
        confirm["host"] = "由插件市场解析"
    if args.force and not args.yes:
        confirm["force_warning"] = "--force 仅在用户确认（--yes）后生效"
    return confirm, problems


# ── 子命令：install / enable（预览默认，确认后执行）──────────────────────

def _build_install_argv(args: argparse.Namespace) -> list[str]:
    argv = ["install", "--type", args.kind]
    if args.kind == "plugin":
        code = args.plugin_code or args.slug
        if not code:
            raise ValueError("插件安装需要 --plugin-code 或 --slug")
        argv += ["--plugin-code", code]
    else:
        if not args.slug:
            raise ValueError("技能安装需要 --slug")
        argv += ["--slug", args.slug]
        if args.source:
            argv += ["--source", args.source]
        if args.download_url:
            argv += ["--download-url", args.download_url]
        if args.version:
            argv += ["--version", args.version]
        if args.skill_id:
            argv += ["--skill-id", args.skill_id]
        if args.version_id:
            argv += ["--version-id", args.version_id]
    if args.force:
        argv += ["--force"]
    return argv


def _emit(action: str, ok: bool, **fields) -> None:
    print(SENTINEL_BEGIN)
    print(json.dumps({"action": action, "ok": ok, **fields}, ensure_ascii=False))
    print(SENTINEL_END)


def _extract_finder_json(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def cmd_install(args: argparse.Namespace) -> int:
    """安装候选：默认只做策略校验与预览（不执行），用户确认（--yes）后才真正执行。"""
    use_skillhub = getattr(args, "source", None) == "skillhub"
    if use_skillhub and not (args.slug or "").strip():
        _emit("install", False, error="SkillHub 安装需要 --slug", error_type="arguments")
        return 1
    try:
        extra = [] if use_skillhub else _build_install_argv(args)
    except ValueError as exc:
        log(str(exc), level="error")
        _emit("install", False, error=str(exc), error_type="arguments")
        return 1

    confirm, problems = _install_policy(args)
    if problems:
        _emit("install", False, error="; ".join(problems), error_type="policy", confirm=confirm)
        return 2

    if use_skillhub:
        skillhub_bin = shutil.which("skillhub")
        install_dir = (getattr(args, "dir", None) or "").strip() or _skillhub_install_dir()
        cli_argv = ["install", args.slug]
        if getattr(args, "namespace", None):
            cli_argv += ["--namespace", args.namespace]
        if install_dir:
            cli_argv += ["--dir", install_dir]
        command = [skillhub_bin or "skillhub", *cli_argv]
        if args.dry_run or not args.yes:
            confirm["install_dir"] = install_dir
            _emit("install", True, executed=False, dry_run=True, needs_confirmation=True,
                  confirm=confirm, command=command)
            return 0
        if skillhub_bin is None:
            _emit("install", False,
                  error="未找到 skillhub CLI（安装方式见 references/installer-contract.md）",
                  error_type="cli_missing")
            return 1
        if install_dir is None:
            _emit("install", False,
                  error="未确定技能安装目录：请传 --dir 或设置 SKILLHUB_INSTALL_DIR",
                  error_type="arguments")
            return 1
        log(f"install（已经用户确认，经 SkillHub CLI）: {' '.join(cli_argv)}")
        try:
            proc = subprocess.run(
                command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            _emit("install", False, error=f"安装超时（{args.timeout}s）", error_type="timeout")
            return 2
        ok = proc.returncode == 0
        if ok:
            _record_install(args.slug, {
                "kind": "skill",
                "source": "skillhub",
                "namespace": getattr(args, "namespace", None) or None,
                "version": args.version or None,
                "install_dir": install_dir,
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        _emit("install", bool(ok), executed=True, exit_code=proc.returncode, command=command,
              result=None, stderr=(proc.stderr or "").strip()[-2000:] or None)
        return 0 if ok else 2

    # 提供了 --sha256 时先下载校验，通过后以本地文件交给安装器（契约见 references/installer-contract.md）。
    tmp_dir: tempfile.TemporaryDirectory | None = None
    sha_mode = None
    if args.kind == "skill" and args.sha256 and args.download_url:
        tmp_dir = tempfile.TemporaryDirectory(prefix="autoskill_verify_")
        try:
            local = _fetch_and_verify(args.download_url, args.sha256, Path(tmp_dir.name))
        except ValueError as exc:
            tmp_dir.cleanup()
            _emit("install", False, error=str(exc), error_type="hash", confirm=confirm)
            return 2
        except (urllib.error.URLError, OSError) as exc:
            tmp_dir.cleanup()
            _emit("install", False, error=f"下载失败: {exc}", error_type="download", confirm=confirm)
            return 2
        extra = [local.as_uri() if part == args.download_url else part for part in extra]
        sha_mode = "verified_local"

    finder_path = locate_finder()
    command = [sys.executable, str(finder_path) if finder_path else "<extension_finder.py>", *extra]

    if args.dry_run or not args.yes:
        confirm["sha_mode"] = sha_mode
        _emit("install", True, executed=False, dry_run=True, needs_confirmation=True,
              confirm=confirm, command=command)
        if tmp_dir:
            tmp_dir.cleanup()
        return 0

    if finder_path is None:
        if tmp_dir:
            tmp_dir.cleanup()
        _emit("install", False, error="未找到能力发现入口（extension_finder 脚本）", error_type="finder_missing")
        return 1

    log(f"install（已经用户确认）: {' '.join(extra)}")
    try:
        proc = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        _emit("install", False, error=f"安装超时（{args.timeout}s）", error_type="timeout")
        return 2
    finally:
        if tmp_dir:
            tmp_dir.cleanup()

    summary = _extract_finder_json(proc.stdout) or {}
    ok = proc.returncode == 0 and summary.get("ok", True)
    if ok:
        if args.kind == "plugin":
            key = args.plugin_code or args.slug
        else:
            key = args.slug
        _record_install(key, {
            "kind": args.kind,
            "source": getattr(args, "source", None),
            "version": args.version or args.version_id or None,
            "sha256": args.sha256 or None,
            "downloadURL": args.download_url,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    _emit(
        "install", bool(ok),
        executed=True,
        exit_code=proc.returncode,
        command=command,
        result=summary or None,
        stderr=(proc.stderr or "").strip()[-2000:] or None,
    )
    return 0 if ok else 2


def cmd_enable(args: argparse.Namespace) -> int:
    """启用候选：默认预览；台账外（可能被用户有意禁用的）候选需 --user-confirmed。"""
    if args.kind == "plugin":
        key = args.plugin_code or args.slug
        if not key:
            _emit("enable", False, error="插件启用需要 --plugin-code 或 --slug", error_type="arguments")
            return 1
    else:
        key = args.slug
        if not key:
            _emit("enable", False, error="技能启用需要 --slug", error_type="arguments")
            return 1

    managed = key in _load_ledger()
    confirm = {"kind": args.kind, "slug": key, "managed_by_this_flow": managed}
    if not managed:
        confirm["note"] = "该候选不是经本流程安装的，可能是用户有意禁用；重新启用需用户明确同意（--user-confirmed）"

    if args.dry_run or not args.yes:
        _emit("enable", True, executed=False, dry_run=True, needs_confirmation=True, confirm=confirm)
        return 0

    if not managed and not args.user_confirmed:
        _emit("enable", False,
              error="拒绝执行：该候选可能被用户有意禁用，需用户明确要求重新启用（--user-confirmed）",
              error_type="enable_guard", confirm=confirm)
        return 2

    finder_path = locate_finder()
    if finder_path is None:
        _emit("enable", False, error="未找到能力发现入口（extension_finder 脚本）", error_type="finder_missing")
        return 1

    argv = ["enable", "--type", args.kind]
    if args.kind == "plugin":
        argv += ["--plugin-code", key]
    else:
        argv += ["--slug", key]
    if args.force:
        argv += ["--force"]
    command = [sys.executable, str(finder_path), *argv]

    log(f"enable（已经用户确认）: {' '.join(argv)}")
    try:
        proc = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        _emit("enable", False, error=f"启用超时（{args.timeout}s）", error_type="timeout")
        return 2

    summary = _extract_finder_json(proc.stdout) or {}
    ok = proc.returncode == 0 and summary.get("ok", True)
    _emit(
        "enable", bool(ok),
        executed=True,
        exit_code=proc.returncode,
        command=command,
        result=summary or None,
        stderr=(proc.stderr or "").strip()[-2000:] or None,
    )
    return 0 if ok else 2


# ── 参数解析与入口 ────────────────────────────────────────────────────────

def _parse_queries(values: list[str]) -> list[str]:
    queries: list[str] = []
    for value in values:
        value = (value or "").strip()
        if value and value not in queries:
            queries.append(value)
    if not queries:
        raise ValueError("至少提供一个非空 --query")
    if len(queries) > MAX_QUERIES:
        raise ValueError(f"最多提供 {MAX_QUERIES} 个不同 --query")
    return queries


def _search_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="search", description="分层降级检索并按匹配度择优")
    ap.add_argument("--query", action="append", default=[], help="检索词，可重复最多 3 次")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="每个检索词在单个来源的返回上限")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help="最终输出候选数量")
    ap.add_argument("--no-filter", action="store_true", help="关闭噪声过滤，返回原始候选")
    args = ap.parse_args(argv)

    try:
        queries = _parse_queries(args.query)
    except ValueError as exc:
        log(str(exc), level="error")
        print(SENTINEL_BEGIN)
        print(json.dumps({"ok": False, "errors": {"arguments": str(exc)}}, ensure_ascii=False))
        print(SENTINEL_END)
        return 1

    limit = args.limit if args.limit > 0 else DEFAULT_LIMIT
    top = args.top if args.top > 0 else DEFAULT_TOP

    result = run_search(queries, limit=limit, top=top, do_filter=not args.no_filter)

    for key, err in result["errors"].items():
        log(f"{key}: {err}", level="warn")
    log(f"queries={result['query']} active_layer={result['active_layer']} "
        f"candidates={len(result['candidates'])} filter={result['filter']}")

    print(SENTINEL_BEGIN)
    print(json.dumps(result, ensure_ascii=False))
    print(SENTINEL_END)

    # 通用兜底也算跑通：入口缺失 / 无匹配等情况都如实体现在 errors 与 active_layer 里。
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = "search"
    if argv and argv[0] in ("search", "install", "enable"):
        command = argv[0]
        argv = argv[1:]

    if command == "search":
        return _search_main(argv)

    if command == "install":
        ap = argparse.ArgumentParser(prog="install", description="安装候选：默认只预览与策略校验，--yes 后才执行")
        ap.add_argument("--type", dest="kind", choices=["skill", "plugin"], default="skill")
        ap.add_argument("--slug")
        ap.add_argument("--plugin-code")
        ap.add_argument("--source", choices=["square", "hub", "skillhub"])
        ap.add_argument("--namespace", help="SkillHub 专用：候选 ownerName（namespace）")
        ap.add_argument("--dir", help="SkillHub 专用：技能安装目录（默认自动探测）")
        ap.add_argument("--download-url", dest="download_url")
        ap.add_argument("--version")
        ap.add_argument("--skill-id")
        ap.add_argument("--version-id")
        ap.add_argument("--sha256", help="工件 sha256；提供时先下载校验，再以本地文件安装")
        ap.add_argument("--allow-unpinned", action="store_true", help="知情豁免：安装未固定版本的候选")
        ap.add_argument("--allow-no-hash", action="store_true", help="知情豁免：平台无哈希时不做哈希校验")
        ap.add_argument("--force", action="store_true", help="覆盖安装（仅在 --yes 确认后生效）")
        ap.add_argument("--yes", action="store_true", help="用户已确认，真正执行（默认只预览）")
        ap.add_argument("--dry-run", action="store_true", help="显式预览（与默认行为相同，不实际安装）")
        ap.add_argument("--timeout", type=int, default=120, help="安装超时秒数")
        return cmd_install(ap.parse_args(argv))

    ap = argparse.ArgumentParser(prog="enable", description="启用候选：默认预览；台账外候选需 --user-confirmed")
    ap.add_argument("--type", dest="kind", choices=["skill", "plugin"], default="skill")
    ap.add_argument("--slug")
    ap.add_argument("--plugin-code")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--yes", action="store_true", help="用户已确认，真正执行（默认只预览）")
    ap.add_argument("--user-confirmed", dest="user_confirmed", action="store_true",
                    help="用户明确要求重新启用（用于台账外、可能被用户有意禁用的候选）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=60)
    return cmd_enable(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())