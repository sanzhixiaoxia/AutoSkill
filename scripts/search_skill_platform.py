#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动加载技能（AutoSkill）的检索 / 安装入口（分层降级 + 噪声过滤 + 非交互安装）。

分层降级链（按顺序，命中即停）：
  1. 优质平台  quality_platform   —— skill 技能平台（skill hub）
  2. 其他远端  other_remote       —— 技能广场（square）等远端来源
  3. 已装本地  local_installed    —— 本机已安装的 skill / 套件（离线扫描）
  4. 通用工具  generic_tools      —— 以上都没有时，交给通用工具兜底

检索默认对候选做**噪声过滤**：剔除名称/标识无信息量（如纯数字、纯符号、占位名）的垃圾条目，
以及只靠零星字符蹭到、并不对口的弱相关条目；过滤数量在 filter 字段如实报告。
需要看原始候选时加 --no-filter 关闭过滤。

子命令（无交互、不弹窗）：
  search（默认）  分层检索并按匹配度择优
  install         非交互安装候选（复用宿主能力发现入口的 install；stdin 关闭，绝不等待输入）
  enable          非交互启用已下载但被禁用的候选

用法：
  python3 scripts/search_skill_platform.py \
      --query "整理会议记录" --query "会议纪要" [--limit 5] [--top 5] [--no-filter]
  python3 scripts/search_skill_platform.py install --slug <slug> --source hub --download-url <url>
  python3 scripts/search_skill_platform.py install --type plugin --plugin-code <code>
  python3 scripts/search_skill_platform.py install --slug <slug> --source hub --dry-run   # 只打印将要执行的命令

输出：sentinel 包裹的单行 JSON，便于宿主解析：

  ===SKILL_PLATFORM_CANDIDATES===
  {"query":[...],"active_layer":"...","fallback_level":1,"candidates":[...],...}
  ===END===

退出码：0=脚本跑通（候选可能为空，此时 active_layer 为 generic_tools）；
1=参数错误或子命令用法错误；2=所有检索层都失败（不再伪装成“平台没有匹配”）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]

# skill 技能平台（hub）的检索索引标识；与平台检索协议一致。
HUB_INDEX = "clawHub"
# 广场（square）检索索引：个人版 / 企业版不同；与宿主一致。
SQUARE_INDEX_PERSONAL = "baiduMate"
SQUARE_INDEX_ENTERPRISE = "enterprise"

SENTINEL_BEGIN = "===SKILL_PLATFORM_CANDIDATES==="
SENTINEL_END = "===END==="

MAX_QUERIES = 3
DEFAULT_LIMIT = 5
DEFAULT_TOP = 5
DESC_VIEW = 2000

LAYER_QUALITY = "quality_platform"
LAYER_REMOTE = "other_remote"
LAYER_LOCAL = "local_installed"
LAYER_GENERIC = "generic_tools"

LAYER_LABELS = {
    LAYER_QUALITY: "优质平台（skill 技能平台 / hub）",
    LAYER_REMOTE: "其他远端来源（技能广场等）",
    LAYER_LOCAL: "已装本地能力",
    LAYER_GENERIC: "通用工具兜底",
}
FALLBACK_LEVEL = {
    LAYER_QUALITY: 1,
    LAYER_REMOTE: 2,
    LAYER_LOCAL: 3,
    LAYER_GENERIC: 4,
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
            cands.extend(sorted(root.glob(f"*/{rel}")))
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


def filter_and_score(items: list[dict], queries: list[str], *, do_filter: bool) -> tuple[list[dict], int, int]:
    """对候选做噪声过滤 + 相关性判定 + 打分，返回 (保留项, 垃圾数, 弱相关数)。"""
    kept: list[dict] = []
    dropped_junk = 0
    dropped_irrelevant = 0
    for item in items:
        name = item.get("name") or ""
        slug = item.get("slug") or ""
        desc = item.get("description") or ""
        if do_filter and is_junk(name, slug, desc):
            dropped_junk += 1
            continue
        score, matched = score_candidate(queries, name, desc)
        strong = [t for t in queries if _relevant(t, name, desc)]
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
        if v in (">-", ">", "|", "|-", "|+"):
            v = ""
        data[k] = v.strip().strip('"').strip("'")
    return data


def scan_local_layer() -> tuple[list[dict], dict]:
    """离线扫描本机已安装的 skill 与套件，作为第 3 层候选。"""
    items: list[dict] = []
    errors: dict[str, str] = {}
    seen: set[str] = set()

    for root in _skills_roots():
        if not root.is_dir():
            continue
        for tier in sorted(p for p in root.iterdir() if p.is_dir()):
            for skill_dir in sorted(p for p in tier.iterdir() if p.is_dir()):
                md = skill_dir / "SKILL.md"
                if not md.is_file():
                    continue
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

    finder_path = locate_finder()
    finder = None
    runtime = None
    finder_err = None
    if finder_path is None:
        finder_err = "未找到技能检索入口（extension_finder 脚本）；远端层不可用，仅本地层可用"
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

    def record_layer(name: str, *, status: str, items: list[dict], errors: dict, ok_jobs: int) -> bool:
        kept, dj, di = filter_and_score(items, queries, do_filter=do_filter)
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

    # 第 1 层：优质平台（skill hub）
    if finder is not None:
        items, errors, ok_jobs = collect_remote_layer(
            finder, queries, runtime=runtime,
            sources=[{"source": "skill_platform", "index": HUB_INDEX, "kind": "skill"}],
            limit=limit)
        status = "ok" if ok_jobs > 0 else ("error" if errors else "ok")
        log(f"layer={LAYER_QUALITY} raw={len(items)} errors={len(errors)}")
        if record_layer(LAYER_QUALITY, status=status, items=items, errors=errors, ok_jobs=ok_jobs):
            return _finalize(result)

    # 第 2 层：其他远端（广场 skill + 广场/插件来源）
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
        if record_layer(LAYER_REMOTE, status=status, items=items, errors=errors, ok_jobs=ok_jobs):
            return _finalize(result)

    # 第 3 层：已装本地能力（离线扫描）
    if not result["candidates"]:
        items, errors = scan_local_layer()
        log(f"layer={LAYER_LOCAL} raw={len(items)} errors={len(errors)}")
        if record_layer(LAYER_LOCAL, status="ok", items=items, errors=errors, ok_jobs=1):
            return _finalize(result)

    # 第 4 层：通用工具兜底（无候选，交由模型用通用工具）
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
    # 若所有远端层都失败、且本地层也没命中，仍然算跑通（进入通用兜底），但保留 errors 供上层判断。
    return result


# ── 子命令：install / enable（非交互）─────────────────────────────────────

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
    """非交互安装候选：调用宿主能力发现入口的 install，stdin 关闭，绝不等待输入。"""
    try:
        extra = _build_install_argv(args)
    except ValueError as exc:
        log(str(exc), level="error")
        _emit("install", False, error=str(exc), error_type="arguments")
        return 1

    finder_path = locate_finder()
    if finder_path is None:
        _emit("install", False, error="未找到能力发现入口（extension_finder 脚本）", error_type="finder_missing")
        return 1

    command = [sys.executable, str(finder_path), *extra]
    if args.dry_run:
        _emit("install", True, dry_run=True, command=command)
        return 0

    log(f"install（非交互）: {' '.join(extra)}")
    try:
        proc = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        _emit("install", False, error=f"安装超时（{args.timeout}s）", error_type="timeout")
        return 2

    summary = _extract_finder_json(proc.stdout) or {}
    ok = proc.returncode == 0 and summary.get("ok", True)
    _emit(
        "install", bool(ok),
        exit_code=proc.returncode,
        command=command,
        result=summary or None,
        stderr=(proc.stderr or "").strip()[-2000:] or None,
    )
    return 0 if ok else 2


def cmd_enable(args: argparse.Namespace) -> int:
    """非交互启用已下载但被禁用的候选。"""
    argv = ["enable", "--type", args.kind]
    if args.kind == "plugin":
        code = args.plugin_code or args.slug
        if not code:
            _emit("enable", False, error="插件启用需要 --plugin-code 或 --slug", error_type="arguments")
            return 1
        argv += ["--plugin-code", code]
    else:
        if not args.slug:
            _emit("enable", False, error="技能启用需要 --slug", error_type="arguments")
            return 1
        argv += ["--slug", args.slug]
    if args.force:
        argv += ["--force"]

    finder_path = locate_finder()
    if finder_path is None:
        _emit("enable", False, error="未找到能力发现入口（extension_finder 脚本）", error_type="finder_missing")
        return 1

    command = [sys.executable, str(finder_path), *argv]
    if args.dry_run:
        _emit("enable", True, dry_run=True, command=command)
        return 0

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

    if result["errors"].get("finder") and result["active_layer"] == LAYER_GENERIC and not result["candidates"]:
        # 入口缺失 + 本地无匹配：仍然跑通到通用兜底，但如实返回 0（调用方可看 errors/active_layer）。
        return 0
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
        ap = argparse.ArgumentParser(prog="install", description="非交互安装候选（不弹窗）")
        ap.add_argument("--type", dest="kind", choices=["skill", "plugin"], default="skill")
        ap.add_argument("--slug")
        ap.add_argument("--plugin-code")
        ap.add_argument("--source", choices=["square", "hub"])
        ap.add_argument("--download-url", dest="download_url")
        ap.add_argument("--version")
        ap.add_argument("--skill-id")
        ap.add_argument("--version-id")
        ap.add_argument("--force", action="store_true")
        ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令，不实际安装")
        ap.add_argument("--timeout", type=int, default=120, help="安装超时秒数")
        return cmd_install(ap.parse_args(argv))

    ap = argparse.ArgumentParser(prog="enable", description="非交互启用已签入但被禁用的候选")
    ap.add_argument("--type", dest="kind", choices=["skill", "plugin"], default="skill")
    ap.add_argument("--slug")
    ap.add_argument("--plugin-code")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=60)
    return cmd_enable(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())