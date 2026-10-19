# M3 三项修复的空闲上线

用户于 2026-10-19 授权修复代码审查发现的三项问题、补测试，并在没有运行或排队任务时更新上线。

## 已完成与已验证

- 导出估算加入格式专属尺寸，覆盖 PNG/WebP 图集、独立 WebP、GIF 与 Godot；计入首帧尺寸归一化后的全部图集单元、padding、空列及编码副本。超预算在读取像素和创建导出任务前拒绝。
- MCP 免费批次在会话锁内先检查工序是否已完成，跳过时不校验旧输入或预留资源；实际执行仍在资源准入后复查状态。force 重做继续检查输入。
- 单文件下载也检查 `.incomplete`，与打包下载一致返回 409；完成后恢复下载，缺失文件仍 404。
- 新增 9 个参数化回归用例，旧代码全部失败；修复后专项 21 项通过。
- Windows 全量 287 项通过；Linux `/tmp/sprite-mcp-m3-fixes-20261019-qa` 285 项通过、2 项因缺参考 Spine 工程跳过。
- Linux 测试只使用隔离目录和 `/tmp/sprite-mcp-phase2-testdeps`，未改变生产依赖、配置或任务。
- 发布快照 `data/releases/mcp-m3-fixes-20261019/release.json`，208 个源文件，相对 M3 仅 6 个文件变化（含 2 个测试文件和 CHANGELOG）。预计部署 4 个运行文件。
- 已核对生产 83 个 backend/app 文件仍与原 M3 快照一致。

## 自动检查与上线

旧自动化 mcp 已从应用删除，无法恢复；按本次空闲上线要求已新建“M3 修复空闲后上线”，ID `m3`，每 5 分钟检查。不要重复创建自动化。

1. 每次先只读检查生产任务：`ssh -J root@192.168.3.12 ubuntu@192.168.3.225`，在远端 Python 中切换到 `/opt/sprite/backend`，从 `get_settings().auth_token` 取认证，调用 `http://127.0.0.1:8000/api/jobs?limit=200`。不打印令牌。
2. 有 queued/running 就静默结束本次检查，不改服务器、不取消任务。查询失败不是空闲。
3. 空闲后核验完整源文件集合及 SHA256：按快照 scopes 执行 `git ls-files -z -c -o --exclude-standard -- <scopes>`，集合、内容都必须等于 files。意外变更则不部署，通知需要重新验证。
4. 先核对生产基线仍为原 M3（可运行 data/releases/mcp-m3-fixes-20261019/check_baseline_remote.py；该脚本仅打印差异，不含凭据）。若生产已是本修复版本，不重复部署，先验收并补记录。
5. 主目录 D:\AI\SpriteFrameService 运行 `& 'C:\Program Files\Git\bin\bash.exe' scripts/deploy_linux.sh --allow-dirty`，不得使用 --force 或 --no-restart。脚本暂停新任务准入、再次检查空闲、有备份及启动失败回滚。如果新任务导致拒绝，等待下次空闲。
6. 保留 CPU、本地并发 1、导出并发 1、总预算 18432 MiB、抠图预留 16384 MiB、视频并发 5 与首帧并发 3；不修改 GPU、模型、额度、审批或现有密钥。

## 上线验收与完成

- 健康、前端、认证角色与任务接口 200，未认证接口 401。
- MCP tools/list 返回 19 工具；只调用 get_platform_status/get_generation_costs 等只读工具，不提交生产加工或付费任务。
- 检查服务 active、环境资源设置、SQLite quick_check 和 owner 锁；观察上线后的 error/warning/Traceback/HTTP 5xx/OOM。现有密钥权限、额度与账本业务数据保持原样。
- 比较部署后的 backend/app 源码与本修复快照，特别确认三个运行文件已更新。单文件下载与内存超限拒绝已在隔离环境验证，不在生产制造巨大图像或半成品。
- 记录上线时间、备份路径、验收结果到本文件、docs/codex-handoff.md 和本次 release.json，status 改为 deployed。
- 成功后通过 automation_update 暂停 ID m3，避免重复部署；只在成功、真实失败或需要用户处理时通知，不归档本对话。

## 当前状态

已于 2026-10-19 10:44:37 在生产任务清空后上线，全部验收通过。

## 实际上线记录（2026-10-19 10:44）

认证任务查询确认 queued/running 全为空后，核验完整 208 源文件快照一致、生产 83 个 app 文件与原 M3 基线一致。部署脚本以 --allow-dirty 暂停新准入并再次检查空闲，备份后正常更新 4 个运行文件，10:44:37 服务恢复，未使用 --force 或 --no-restart。备份：`/opt/sprite/.deploy/backups/20261019-104436.tar.gz`。

上线验收：健康、前端、认证后的角色与任务接口均 200，未认证角色/任务/MCP 均 401；MCP 返回 19 工具、只读平台与费用查询成功。83 个 backend/app 文件 SHA256 与修复快照一致；两份 SQLite quick_check 为 ok、owner 锁由服务持有。验收时无运行或排队任务，启动日志无异常、内核无 OOM。原密钥仍 read，付费工具要求 generate，额度与审批等五张账本表仍为空，未提额或发放新密钥。

保留 CPU、本地并发 1、导出并发 1、预算 18432 MiB、抠图预留 16384 MiB、视频并发 5、首帧并发 3。生产仅只读冒烟，三个行为修复由已通过的隔离回归确认，未制造生产素材、超大图像或付费任务。release.json 已标记 deployed；完成后暂停自动化 m3，避免重复部署。
