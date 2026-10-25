# 安装器调用契约与审计说明

本技能不直接实现下载与安装，而是调用宿主提供的能力发现入口（extension-finder 脚本）完成安装与启用。本文件把这条调用链的接口契约、安全策略与可审计入口固化在包内，便于审阅与升级。

## 一、入口定位（locate_finder）

按优先级尝试：

1. 环境变量 `SKILL_PLATFORM_FINDER` 指定的脚本路径；
2. `QIANFAN_SKILLS` 根目录下 `official` / `user` 层级中的 `extension-finder/scripts/extension_finder.py`；
3. 从本技能目录向上回推到目录名为 `skills` 的根，同上布局。

任一入口不可用时，检索自动降级到本地层与通用兜底，安装如实报 `finder_missing`。

## 二、install 子进程契约

- 命令：`<python> <extension_finder.py> install --type skill|plugin [--slug S] [--source hub|square] [--download-url U] [--version V] [--skill-id I] [--version-id VID] [--plugin-code C] [--force]`
- 进程属性：`stdin=DEVNULL`（不交互、不弹窗）、stdout/stderr 捕获、超时（默认 120s，`--timeout` 可调）。
- 结果解析：取 stdout 中第一行完整 JSON 作为安装摘要；返回码非 0 或摘要 `ok=false` 视为失败。
- **本地已验证安装**：调用方提供 `--sha256` 时，本技能先自行下载（https）或读取（file://）工件、校验哈希，再把**本地文件的 file:// URI** 作为 `--download-url` 传给安装器。契约要求：宿主安装器必须支持 file:// 本地路径；若不支持，安装会失败并如实报错，**不会回退到直接安装远端 URL**（防止校验过的字节与实际安装的字节不一致）。

## 三、enable 子进程契约

- 命令：`<python> <extension_finder.py> enable --type skill|plugin (--slug S | --plugin-code C) [--force]`
- 进程属性同上（默认 60s 超时）。

## 三B、SkillHub CLI 契约（source=skillhub 时）

SkillHub 是免鉴权的开放技能社区，不依赖宿主 extension-finder，任何环境可用。

- **检索**：`GET https://api.skillhub.cn/api/skills?keyword=<词>&sortBy=score&pageSize=<n>`（https 固定官方域，无需鉴权；每词一次、并发、按 slug 去重）。返回 `data.skills[]`，取 `slug / name / description(_zh) / version / ownerName / downloads` 归一为候选，`ownerName` 即安装用的 namespace。
- **安装**：`skillhub install <slug> --namespace <ownerName> --dir <技能目录>`，经 `shutil.which("skillhub")` 定位 CLI（缺失时如实报 `cli_missing`，安装方式：`curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --cli-only`）。进程属性与 finder 路径一致（stdin 关闭、捕获输出、`--timeout`）。
- **安装目录**：`--dir` 参数 > `SKILLHUB_INSTALL_DIR` 环境变量 > 已知技能根（`QIANFAN_SKILLS` 等）> `~/.zcode/skills`（存在时）。
- **策略差异**：下载与完整性由 skillhub CLI 官方源管理，不走 finder 路径的 URL 白名单 / 版本固定 / 哈希三项强制校验；**预览与 `--yes` 确认流程与 finder 路径完全一致**，成功同样记入安装台账。

## 四、安全策略（默认生效）

1. **确认优先**：`install` / `enable` 默认只做策略校验与预览（`executed=false`），加 `--yes` 才真正执行；预览返回 `confirm`（来源、版本、下载地址、将执行的命令），供向用户展示并取得同意。
2. **域名白名单**：skill 下载地址必须 https 且域名命中白名单（后缀匹配，允许子域）。默认表见脚本 `DEFAULT_ALLOWED_HOSTS`（bcebos.com、github.com 系）；环境变量 `SKILL_INSTALL_ALLOWED_HOSTS` 可整体替换（逗号分隔，设了就完全替换默认表）。
3. **版本固定**：skill 安装必须携带 `--version`（或 `--skill-id` / `--version-id`）；缺失时拒绝，除非用户知情后显式 `--allow-unpinned`。
4. **哈希校验**：提供 `--sha256` 即先校验后安装（见第二节）；平台无哈希时需显式 `--allow-no-hash` 表示用户知情。
5. **--force 受限**：仅在 `--yes`（用户确认）后随安装命令生效。
6. **enable 保护**：安装台账（`scripts/.install-ledger.json`，记录经本流程安装的候选）之外的候选视为"可能被用户有意禁用"，重新启用必须额外携带 `--user-confirmed`，否则拒绝（`error_type=enable_guard`）。

## 五、可审计入口（脚本内实现位置）

| 关注点 | 函数 |
| --- | --- |
| 白名单与策略校验 | `_allowed_hosts` / `_host_allowed` / `_install_policy` |
| 下载与哈希校验 | `_fetch_and_verify` |
| 安装/启用执行与台账写入 | `cmd_install` / `cmd_enable` / `_record_install` |
| 传给安装器的参数拼装 | `_build_install_argv` |
| 输出协议（sentinel 单行 JSON） | `_emit`（`executed` / `dry_run` / `confirm` / `error_type` 字段） |

## 六、运行时数据披露（流程责任，非脚本责任）

脚本不拦截能力运行时行为。新装能力首次调用前"将接收哪些用户数据"的披露与拒绝权，由技能流程承担（见 `../SKILL.md` 第四节第 9 条），不在本契约范围内。
