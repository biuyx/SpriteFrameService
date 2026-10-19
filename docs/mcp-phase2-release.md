# MCP 二期上线操作

用户已授权开发并在**没有运行及排队任务时上线**。已于 2026-10-16 15:18:25（北京时间）在队列清空后上线并完成验收。
自动化名称为“MCP 二期空闲后上线”，ID 为 `mcp`，每 5 分钟在本对话检查一次。

## 已验证版本

- 版本快照：`data/releases/mcp-phase2-20261016/release.json`，记录 204 个源文件的 SHA256 和完整作用域。
- Windows：262 项通过。
- Linux 临时目录 `/tmp/sprite-mcp-phase2-qa`：260 项通过，2 项因参考工程不存在跳过。
- 测试依赖只放在 `/tmp/sprite-mcp-phase2-testdeps`，未改变生产虚拟环境。
- 部署预演通过，预计 26 个文件变化；没有修改线上代码。
- 工作区中包含之前已上线的中文错误提示改动；不要丢弃这些改动，也不要把它们当成本次新问题。

## 空闲检查

通过 `ssh -J root@192.168.3.12 ubuntu@192.168.3.225` 使用已有 SSH 密钥。
在远端 Python 中切到 `/opt/sprite/backend`，从 `app.config.get_settings` 读取认证令牌，使用 urllib
请求 `http://127.0.0.1:8000/api/jobs?limit=200`。只打印任务 ID、类型、状态与进度，不打印凭据。

只要有 `queued` 或 `running`，本次保持静默，继续下次检查。查询失败不等于空闲。
本次最后检查（14:32）仍有一个抠图运行，另有抠图、导出和抽帧排队；这些不是失败状态。

不要在单独 Python 进程里调用新版本 `JobManager.initialize/get/list` 查询生产任务；任务库有唯一调度进程锁。
查询生产任务应经过 HTTP。不要取消任务、自动重发生成、改变任务次序或强制重启。

## 源代码核验

读取快照中的 `scopes`，用 `git ls-files -z -c -o --exclude-standard -- <scopes...>` 获取当前完整文件集合，
去重后与快照 `files` 键集合比较，再逐个比较 SHA256。必须同时检查新增/删除文件与内容变化。
若不一致，停止部署并说明需要重新测试；不能只核对已存在的路径。
快照存于被 Git 忽略的 data 目录，不是源文件的一部分；新建交接文档也不影响发布范围。

## 上线步骤

1. 确认队列空闲、快照完全匹配。
2. 在 VM 创建 `/etc/systemd/system/sprite.service.d/resource-budget.conf`，只添加以下配置，不覆盖现有
   `recovery-cpu.conf` 或 `cuda.conf`。若该文件已存在且内容不同，先确认变动来源。保存需要 sudo。

   ```ini
   [Service]
   Environment=SPRITE_EXPORT_WORKERS=1
   Environment=SPRITE_LOCAL_MEMORY_BUDGET_MB=18432
   Environment=SPRITE_MATTING_MEMORY_MB=16384
   ```

3. `sudo systemctl daemon-reload`。保留 `SPRITE_MAX_WORKERS=1`、`SPRITE_FORCE_CPU=true`、
   `SPRITE_WARMUP_MODEL=false`，不增加物理内存、不加回 GPU、不修改 Ark 或生图并发。
4. 在 Windows 项目根目录执行：

   ```powershell
   & 'C:\Program Files\Git\bin\bash.exe' scripts/deploy_linux.sh --allow-dirty
   ```

   不使用 `--force`、`--no-restart`。脚本会再次检查任务，备份代码、更新并重启，失败会回滚。
   本次允许 dirty 是为了发布已验证、尚未提交的工作区；快照保证它与测试版本一致。
5. 如果任务在检查后进入队列，脚本应拒绝部署。继续等待下一次空闲，不修改业务任务。
   第一次从旧版本升级没有维护准入接口，脚本会输出兼容提示并使用原有空闲检查；以后升级会暂停新任务准入。

## 上线验收

- `sprite.service` active，健康接口和前端 HTTP 200；认证后的角色和任务接口 HTTP 200。
- POST `/api/mcp`，使用 Bearer 及 `Accept: application/json, text/event-stream`，JSON-RPC `tools/list`
  应返回 14 个工具，包含 `plan_processing`、`submit_processing_batch`、`get_processing_batch`、`cancel_processing_job`。
- 可以调用只读 `get_platform_status` 查看调度预算和名额；不提交生产加工或付费任务。
- 检查服务进程环境，确认 CPU、本地并发、导出并发和内存预算均生效。读取环境时只打印上述指定键。
- `data_dir/jobs.sqlite3` 与 `jobs.owner.lock` 应创建。无需打开第二个任务调度器验证。
- 原 read 密钥保持原权限，不自动新增或升级用户密钥。需要加工权限时用户可使用 CLI 发放 `read,operate` 密钥。
- 查看本次启动后的异常日志，确认没有新的任务持久化、权限或启动错误。

记录上线时间、部署备份、验收结果到本文件和交接文档；把快照 `status` 改为 `deployed`，写入验收信息。
暂停 ID `mcp` 的自动化（通过应用 automation_update 工具更新），防止重复执行，不归档对话。
成功或真实失败时通知用户；排队未清空时不要发送重复状态消息。

## 实现边界

任务持久化保存状态、结果及批次计划；进程重启后排队/运行任务变为中断终态，不自动重放。
付费视频仍使用原有素材版本重挂流程，二期没有开放付费 MCP 操作。
同一个 AI 密钥的相同 request_id 重试返回原批次，参数不同则拒绝；明确重做需新编号。
导出独立名额但仍受动作互斥、估算预算及实际可用内存限制，大导出可能合理排队。
整角色 Spine 导出独占内存预算；不同动作的小导出可以与单路抠图并行。

## 实际上线记录（2026-10-16）

15:17 的认证任务查询为空，204 个源文件及完整文件集合与发布快照完全一致。部署脚本再次检查空闲后正常更新 26 个文件，15:18:25 服务恢复，未使用 force 或 no-restart。备份：`/opt/sprite/.deploy/backups/20261016-151823.tar.gz`。

健康、前端、认证后的角色与任务接口均为 HTTP 200；MCP tools/list 返回 14 个工具，含四个二期加工工具。只读平台状态确认总内存预算 18432 MiB；服务环境确认 CPU 强制、本地并发 1、导出并发 1、抠图预留 16384 MiB。SQLite 文件已创建、单进程 owner 锁确由服务持有。验收时任务为空。原 claude-code 密钥仍只有 read，两个写工具要求 operate；未发放或升级任何密钥。启动后的 warning/error journal 无记录，未提交素材加工或付费生成。

M3 仍在独立工作树，未包含在本次部署中。验收结果与上线时间已写回 release.json。
