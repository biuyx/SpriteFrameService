# MCP M3：试产与受控付费量产

M3 在独立分支 `codex/mcp-m3` 开发，基于已通过测试的 MCP 二期工作区副本。
原项目 `D:\AI\SpriteFrameService` 保留二期已验证快照及空闲上线自动化；本目录的 M3 改动没有覆盖该快照。
M3 已集成到主工作目录并于 2026-10-16 15:42:09（北京时间）空闲上线。未给现有密钥增加权限或额度，未发起真实付费任务。

## 使用流程

MCP 新增 5 个工具，总计 19 个。

1. `plan_generation`：传入角色、动作、`steps: ["firstframe", "generate"]`，以及 `mode: "trial"` 或 `"production"`。
   只读预览，已完成步骤默认跳过。重做须明确 `force: true`。
   试产一次仅一个动作；量产一次最多 50 个动作。计划包含解析后的提示词、模型、素材摘要、工艺摘要、草稿/定型状态及调用次数与视频秒数。
2. 管理员审核完整计划，把工具返回的 JSON 对象保存为 `plan.json`。必须与服务器当前素材、提示词、配置及工序状态一致。
3. 管理员用本地 CLI 设置调用方额度并审批计划。调用方使用 AI 密钥记录的 id（`agent_keys.py list`），不能填名字或明文密钥。
4. 拥有 `generate` 权限的调用方使用 `submit_generation_batch(approval_id, request_id)` 提交。
   审批绑定调用方、完整计划及有效期，只能消费一次。网络重试使用相同 request_id；同编号不同审批会拒绝。
5. `get_generation_batch(job_id)` 查询批次；`get_generation_costs()` 查询自身额度、预留及生成尝试记录。
6. 全部试产工序成功后，管理员人工检查图片、角色一致性、动作与镜头节奏，执行 `qualify` 定型。
   同一工艺摘要才能量产；参考素材、提示词版本、模型、视频参数或抽帧规则变化会使新工艺回到草稿。
7. 免费抽帧、抠图和导出继续使用二期 `plan_processing` / `submit_processing_batch`，共享原有资源调度。

付费工具只开放首帧与参考视频生成，不提供任意提示词覆盖或无审批的混合流水线入口。
额度与审批仅约束 MCP 付费入口；网页中现有管理员生成入口维持原有行为。

## 管理员命令

在服务器代码根目录运行，沿用现有后端配置。以下均为示例，开发测试未执行生产审批或额度设置。

```bash
.venv/bin/python scripts/generation_admin.py quota AGENT_ID --calls 10 --seconds 24
.venv/bin/python scripts/generation_admin.py approve AGENT_ID plan.json --ttl 3600
.venv/bin/python scripts/generation_admin.py report AGENT_ID
.venv/bin/python scripts/generation_admin.py qualify LEDGER_BATCH --note '已人工审核角色、边缘与动作节奏'
.venv/bin/python scripts/generation_admin.py revoke APPROVAL_ID
.venv/bin/python scripts/generation_admin.py draft RECIPE_SHA256
```

额度是累计总上限，不是每批额度，不会每日自动重置；未配置时为零。首帧消耗一次调用、零视频秒数；视频消耗一次调用与请求时长。
设置额度不会改变密钥权限；仍须单独签发 `read,generate` 密钥，免费加工还需要 `operate`。
审批默认 1 小时，最长 24 小时。撤销、过期或撤回工艺定型都会阻止后续付费调用，已经发往供应商的请求不保证取消或退款。

## 持久化、记账与恢复

`data_dir/generation.sqlite3` 保存额度、审批、工艺定型、预留与尝试账本；二期 `jobs.sqlite3` 保存执行任务。
备份时应连同两份 SQLite 文件及角色素材/版本一并保存。审批和额度的写入只由服务器本地 CLI 提供，MCP 没有自行审批或提额工具。

提交先以 SQLite 写事务预留整个批次的调用次数和视频秒数；每次越过供应商调用边界前，先持久化一次不可重放的尝试记录。
错误、超时、服务崩溃都不能证明没有计费，因此已尝试额度不会自动退回。正常结束或取消时，只释放尚未尝试的部分。
视频生成沿用 `takes.json` 的远端任务编号和重挂机制；账本同时记录远端任务编号与成功响应中的供应商用量。
同步生图接口没有可查询的远端任务编号，超时记录为未知，不自动重发。

`cancel_generation_batch` 需要 generate 权限及批次所有权，停止后续调用并尝试取消远端视频；免费取消工具不能取消付费批次。
服务重启后任务由二期机制标记中断，不重放；预留继续保留以防重复花费。
管理员确认无需继续后，可以显式封闭批次并释放未尝试额度：

```bash
.venv/bin/python scripts/generation_admin.py close LEDGER_BATCH
```

close 不撤销已经发往供应商的请求；会阻止该批次未来任何调用。已消费的审批不能用新编号重做，需重新规划审批。

金额字段为 `null`，不把未知费用显示为零。报告保存调用次数、请求视频秒数和成功视频的供应商 usage；真实金额以供应商账单核对。
本期没有引入未经核实的固定单价，也不支持用人民币额度代替调用次数额度。

## 上线约束

先完成二期空闲上线，再核验和集成 M3 增量，重新生成 M3 发布快照。不得用二期自动化直接部署本目录。
仍须确认没有 queued/running 任务；使用既有带备份、空闲二次检查及回滚的部署脚本，不使用强制重启。
验收健康接口、认证、19 个工具、原只读/operate 密钥不能付费、未配置额度默认拒绝以及 SQLite 路径。
生产冒烟仅查询，不使用生产素材做生成测试，不自动发放 generate 密钥或审批。

## 测试

新增测试使用真实 SQLite、任务调度、素材写入和视频版本登记，供应商、OSS 与下载均为替身。
覆盖额度并发竞争、重复提交、单次审批、跨调用方隔离、未知计费不退、撤销/过期、试产定型、素材变动失效、取消与 HTTP 权限边界。

2026-10-16 验证结果：Windows 278 项通过；Linux 隔离目录 `/tmp/sprite-mcp-m3-qa-final` 276 项通过、2 项因缺少参考 Spine 工程跳过。
Linux 测试依赖复用 `/tmp/sprite-mcp-phase2-testdeps`，未修改生产虚拟环境或重启服务。新增 16 项 M3 测试均未调用真实供应商。
当前源码快照为本工作目录 `data/releases/mcp-m3-20261016/release.json`（208 个源文件，12 个文件相对二期变动），状态 `tested_not_deployed`。

## 集成与实际上线（2026-10-16）

二期已于 15:18 上线；本次先核验二期基线与 M3 完整快照，再只集成 12 个增量文件。完整 208 源文件集合和 SHA256 与已验证 M3 一致。Windows 集成回归 278 项通过；Linux `/tmp/sprite-mcp-m3-integrated-qa` 276 项通过、2 项缺参考工程跳过。一次迁移测试因临时数据目录位于 D 盘命中原用例的 D 盘路径假设，改用 C 盘隔离目录后完整回归通过，未为此改动业务代码。

等待抠图 49 帧及后续导出完成，认证查询确认 queued/running 均为零。部署脚本暂停新准入并再次检查空闲，正常备份和更新 10 个运行文件，15:42:09 服务恢复。未使用 --force 或 --no-restart。备份：`/opt/sprite/.deploy/backups/20261016-154207.tar.gz`。

验收健康、前端、认证后角色及任务接口均 200，MCP 返回 19 个工具，平台状态及生成用量查询成功；jobs.sqlite3 与 generation.sqlite3 quick_check 为 ok，owner 锁仍由服务持有。账本五张表均为空，默认额度为零，未创建审批/批次/尝试。原 claude-code 密钥文件 SHA256 与上线前一致，权限仍 read；付费提交与取消均要求 generate。

CPU 强制、本地并发 1、导出并发 1、内存预算 18432 MiB、抠图预留 16384 MiB 保持生效，视频生成并发 5、首帧并发 3 未变。启动后 warning/error journal 无记录；生产冒烟只查询，未调用真实付费生成或素材加工。

主目录 `data/releases/mcp-m3-20261016/release.json` 已标记 deployed，包含上线时间、备份和验收记录。没有创建新的等待上线自动化；原二期自动化继续暂停。

## 上线后运行复查（2026-10-16 15:45）

服务 active/running，15:42:09 启动后自动重启 0 次。健康、前端、认证后的角色和任务接口均 200；未认证访问角色、任务和 MCP 均 401。19 个工具及其读写标注正确，已部署 83 个 backend/app 文件 SHA256 与发布快照一致。

检查上线后的 458 条 journal 记录，未发现 warning、error、Traceback、HTTP 5xx 或内核 OOM。4 条上线后 MCP 审计均为平台/费用只读查询，全部成功。两份 SQLite quick_check 为 ok，付费账本五表为空、默认额度为 0，原密钥仍为 read。

当前无运行或排队任务、无失败任务；持久化历史有 6 个 done，不能据此推断这 6 个任务均在 M3 上线后执行。资源名额与预留已清零，本地并发 1、导出并发 1、总预算 18432 MiB、抠图预留 16384 MiB 生效。可用内存 19125 MiB，服务当前约 163 MiB，Swap 未使用。

另在 Linux 隔离目录运行 M3 权限及资源调度专项测试：21 项通过（6.41 秒），覆盖 read/operate 禁止付费、调用方隔离、审批/额度/幂等、异动作导出与抠图共存、同动作互斥、内存不足排队及模型缓存回收。供应商全部为替身，未对生产提交加工或付费任务。当前生产空闲且尚无付费批次；未宣称真实付费负载已经验证。详细结果保存于 data/releases/mcp-m3-20261016/postdeploy-check.json。
