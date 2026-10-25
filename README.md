# AutoSkill · 自动加载技能

只装这一个技能就够：自动分析对话意图，找到并**加载执行**合适的能力——不把"该用哪个技能"抛回给用户，缺的能力直接非交互装好再用。

## 工作原理

1. 判断请求是"直接回答"还是"需要专门能力"（要交付文档/图片/应用等 → 需要专门能力）。
2. 先查宿主注入的可用能力清单，命中即用。
3. 清单没有 → 跑脚本分层检索，**本地优先、网上补足**，命中即停：

   ```
   已装本地 → 优质平台（hub）→ 其他远端（技能广场）→ 通用工具兜底
   ```

4. 拿到能力直接开工：已装的直接加载；远端候选用脚本 `install` / `enable` 装好再用（免确认、不弹窗）。

## 脚本用法

```bash
# 分层检索（--query 最多 3 个；--no-filter 关闭噪声过滤）
python3 scripts/search_skill_platform.py --query "会议纪要" --query "整理会议记录"

# 非交互安装 / 启用（参数照抄候选里的 install_hint）
python3 scripts/search_skill_platform.py install --slug <slug> --source hub --download-url "<url>"
python3 scripts/search_skill_platform.py enable --slug <slug>
```

（Windows 上命令用 `python`。）

输出为 sentinel 包裹的单行 JSON：`active_layer` 指明命中的层，`candidates[].install_hint` 是现成安装参数，`layers` / `filter` / `errors` 如实记录各层与过滤情况。候选排序只是提示，以语义对口为准。

退出码：`0` 跑通（候选可为空，落到通用兜底）；`1` 参数错误；`2` 检索全失败或安装失败/超时。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `SKILL.md` | 技能主文件：路由流程、决策规则 |
| `references/routing-playbook.md` | 易混场景判定、组合编排、降级细则（拿不准时查） |
| `scripts/search_skill_platform.py` | 分层检索、过滤与非交互安装的统一入口 |
| `tests/test_search_skill_platform.py` | 回归测试：`python -m unittest discover -s tests` |
