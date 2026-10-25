# AutoSkill · 自动加载技能

只装这一个技能就够：自动分析对话意图，找到并**加载执行**合适的能力——不把"该用哪个技能"抛回给用户，缺的能力直接非交互装好再用。

## 工作原理

1. 判断请求是"直接回答"还是"需要专门能力"（要交付文档/图片/应用等 → 需要专门能力）。
2. 先查宿主注入的可用能力清单，命中即用。
3. 清单没有 → 跑脚本分层检索，**本地优先、网上补足**，命中即停：

   ```
   已装本地 → 优质平台（hub）→ 其他远端（技能广场）→ SkillHub 开放社区 → 通用工具兜底
   ```

   SkillHub 层免鉴权、不依赖宿主检索入口——宿主入口缺失或不可达时仍能拿到可安装的远端候选。

4. 拿到能力直接开工：已装的直接加载；远端候选先向用户展示**来源、版本、下载地址与哈希情况**，确认后 `install --yes` 安装（脚本默认只预览，策略强制域名白名单、版本固定、哈希校验），首次调用前披露该能力将接收的数据范围。

## 脚本用法

```bash
# 分层检索（--query 最多 3 个；--no-filter 关闭噪声过滤）
python3 scripts/search_skill_platform.py --query "会议纪要" --query "整理会议记录"

# 安装：默认只预览与策略校验；用户确认后原命令加 --yes 才执行
python3 scripts/search_skill_platform.py install --slug <slug> --source hub --download-url "<url>" --version <v>
python3 scripts/search_skill_platform.py install --slug <slug> --source hub --download-url "<url>" --version <v> --yes
# 启用（非本流程安装的候选需 --user-confirmed，保护用户的禁用决定）
python3 scripts/search_skill_platform.py enable --slug <slug> --yes
# SkillHub 候选（经 skillhub CLI 安装到技能目录）
python3 scripts/search_skill_platform.py install --slug <slug> --source skillhub --namespace <owner> --yes
```

策略默认强制：下载地址 https + 域名白名单（`SKILL_INSTALL_ALLOWED_HOSTS` 可配置）、版本固定、`--sha256` 哈希校验；豁免均需显式知情开关（`--allow-unpinned` / `--allow-no-hash`）。

（Windows 上命令用 `python`。）

输出为 sentinel 包裹的单行 JSON：`active_layer` 指明命中的层，`candidates[].install_hint` 是现成安装参数，`layers` / `filter` / `errors` 如实记录各层与过滤情况；本地未命中时附 `near_miss`（弱词法相关的已装候选，语义对口可直接采用）。候选排序只是提示，以语义对口为准。

退出码：`0` 跑通（候选可为空或安装仅预览）；`1` 参数错误；`2` 检索全失败，或安装/启用被策略拦下、失败、超时。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `SKILL.md` | 技能主文件：路由流程、决策规则 |
| `references/routing-playbook.md` | 易混场景判定、组合编排、降级细则（拿不准时查） |
| `references/installer-contract.md` | 宿主安装器调用契约、安全策略与审计入口 |
| `scripts/search_skill_platform.py` | 分层检索、过滤与策略化安装（确认后执行）的统一入口 |
| `tests/test_search_skill_platform.py` | 回归测试：`python -m unittest discover -s tests` |
