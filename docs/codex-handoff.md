# SpriteFrameService 开发交接

## 2026-10-21 同名导出残留修复

详见 `docs/export-replacement-fix.md`。`run_export` 已改为独立暂存、成功后整目录替换，失败/取消保留旧版本；13项新增回归，Windows352通过，Linux隔离350通过/2缺参考工程跳过。店长1的assess/exclusivewait已保留当前49/26帧，72张旧尾帧及49张旧别名产物归档；完整两动作原素材备份 `/opt/sprite/.deploy/export-cleanup-20261021-120608/shop1-actions-original.tar.gz` 已逐文件校验。任务保持管理员暂停、0运行/排队，没有生产加工或重新提交。上线状态在专项文档验收段更新。

交接日期：2026 年 10 月 16 日。用户要求将 Claude 的项目进展与上下文迁到当前 Codex 对话继续执行。

## 当前基线

- 工作目录：`D:\AI\SpriteFrameService`。
- 当前分支：`optimize/risk-fixes`。
- 当前提交：`50af917f498fd4ffd58ef8a83268f8d74d9bd307`，2026 年 10 月 14 日，本地处理统一成一个闸门。
- 接手时工作区干净，没有待接管的未提交代码；本次新增本交接文档。
- 本次实际运行 `.\.venv\Scripts\python.exe -m pytest backend/tests -o addopts='-p no:cacheprovider --tb=short' -q`：**236 passed in 11.06s**。退出码为 0；退出清理历史 pytest 临时目录时有 Windows 目录非空警告。
- 本机进程检查未发现正在运行的 Claude。没有终止任何进程。

## 已完成开发

| 提交 | 内容 |
| --- | --- |
| `50af917` | 本地任务共用 `services/concurrency.py` 的 `local_gate`；直接提交的 cpu 任务和流水线步骤共用并发限额；`SPRITE_MAX_WORKERS` 默认 4 |
| `20da896` | 同一动作并发生成时 `takes.json` 不再丢版本；OSS 缓存并发回写修复 |
| `d386282` | MCP 一期：只读查询、生产就绪检查、图像审核、AI 专用密钥与审计 |
| `f240dc9` | Linux 部署脚本，支持状态查询、预演、回滚、健康检查和任务执行期间的重启保护 |
| `4eb15b6` | Linux 兼容修补：ZIP 中文文件名、Spine 工程选择与时区说明 |
| `b2c5601` | 素材批量导入改为浏览器选择本机文件夹或上传 ZIP |

Claude 在 10 月 13 日的旧回复称 MCP 和部署脚本“未提交”，这已被上述提交取代。是否已部署到线上仍需查询服务器，不能把本地完成视为上线完成。

## 产品与实现约定

项目是 FastAPI 后端与 Vue 3 前端，提供角色与动作管理，以及首帧、视频生成、抽帧、抠图和导出流水线；支持 Spine 骨架与图集导出。前端构建结果由后端托管。以 README、CHANGELOG 和当前代码为实现依据。

核心理念是**先定型，再量产**：先在样板角色上调稳参考视频模板及抽帧规则、版本化提示词、参考首帧集、角色工艺预设和 Spine 模板，再批量套用。个别失败重做对应动作；多个角色同一动作持续失败时，应回到模板解决，避免逐个改提示词绕过。

- 提示词解析层级：模板 > 动作 key > 分组 > 全局。
- 缩放、描边在导出时应用，顺序为缩放后描边；中间帧保持原分辨率。
- 同一动作的本地处理串行，不同动作可并行；生成的版本索引有单独并发保护。
- 三类并发限额分别控制本地处理、视频生成、首帧生图；不要再次引入彼此独立的本地处理闸门。
- 生成接口按次计费。任务记录主要在内存，重启后先查动作与素材版本状态，避免重复提交。
- 测试使用隔离的临时数据目录，见 `backend/tests/conftest.py`。

## MCP 现状与后续规划

入口是 `/api/mcp`，实现位于 `backend/app/mcp_server/`。目前工具全部只读，覆盖角色与动作总览、模板资产、就绪检查、首帧与帧序列及视频审核、任务查询与等待。AI 密钥通过 `scripts/agent_keys.py` 管理，只存摘要，可吊销，只能访问 MCP 入口；审计落在数据目录的 `logs/agent_audit.jsonl`。

Claude 保存的规划为：

1. 一期只读查询与审核，代码已完成并提交。
2. 二期开放免费加工，并引入批次记录。
3. 三期开放试产到量产的付费生成，配合额度、模板定型或草稿标记、成本追踪。

二、三期是历史规划，没有找到本轮明确的实施任务；不能据此认定用户已要求立即开发或发起付费任务。

## 生产环境背景

以下是 Claude 保存的部署上下文，本次未连接服务器核实实时状态：

- PVE 宿主机：`192.168.3.12`；VM 103 `sprite-service`，Ubuntu 24.04，Tesla P4 8 GB。
- 服务地址：`192.168.3.225` 与工作室网段 `192.168.7.225`，服务端口 8000。
- 服务代码：`/opt/sprite`，普通文件副本，没有 Git checkout；虚拟环境 `/opt/sprite/.venv`。
- systemd 服务名为 **`sprite`**，不同于示例配置中的 `spriteframe`。
- 数据：`/data/sprite-data`；模型：`/data/models`；时区：`Asia/Shanghai`。
- Tesla P4 环境使用钉死的 `onnxruntime-gpu 1.18.1` 与 cuDNN 9.1，参见 `backend/requirements-gpu-pascal.txt`；不要直接覆盖安装通用依赖清单。
- 部署使用 `scripts/deploy_linux.sh` 或 `.bat`；本机存在被 Git 忽略的 `scripts/deploy.env`。部署前先查 `--status`，必要时 `--dry-run`；执行任务期间避免强制重启。
- MCP 依赖是否已经安装、线上当前版本和密钥是否已经发放，都需要实际查询后再下结论。

## 历史数据问题

Claude 在 10 月 13 日用开发机数据检查时记录：vendor2 有 19 个动作因模板无抽帧规则阻塞；“走路入住”动作混有 81 帧 640×640 和 41 帧 320×320。这是当时的开发数据观察，不能视为当前线上问题，也未在本次交接中修改素材。

## 续接入口

上下文恢复与测试基线检查已完成。最新可恢复的相关 Claude 主会话最终回复时间为 2026 年 10 月 13 日 21:13（北京时间），而本地提交更新到 10 月 14 日 17:42；两者之间的代码进展以 Git 与 CHANGELOG 补齐。若用户另有更新的 Claude 会话，应补充其最后任务指令，再从对应任务继续。

主要历史来源：本机 Claude 的 `D--JAVA-kozy-art` 项目会话 `20c614da-7943-4450-9d50-61db5ee0fde1`，以及其 `spriteframe-deployment`、`spriteframe-mcp-roadmap` 记忆。交接仅提取本项目相关内容，没有复制凭据或其他项目对话。

## 10 月 16 日故障与恢复

北京时间 10:04:35，PVE 的 PCIe 根端口 `0000:00:03.0` 报不可纠正的致命错误（SDES、Completion Timeout）。下挂的 Tesla P4 `0000:05:00.0` 无法恢复链路，VFIO 通知 QEMU 出现不可恢复错误，VM 103 暂停于 `internal-error`。宿主机内存、存储有余量；当前机箱温度正常，硬件事件日志未发现本次新告警，具体物理诱因仍未确定。

用户授权尝试恢复 VM、GPU 和服务。直接 `qm resume 103` 被 QEMU 拒绝，要求重置；停止再启动后显卡多次重置失败，QEMU 最终因 PCI IRQ 断言退出。临时移除 GPU 直通后 VM 成功启动。原 VM 配置备份在宿主机 `/root/vm103-before-cpu-recovery-20261016-1022.conf`，原直通项为 `hostpci0: 0000:05:00.0,pcie=1`。

CPU 模式首次启动时服务卡在应用启动阶段。增加 `/etc/systemd/system/sprite.service.d/recovery-cpu.conf` 后重启成功，内容为：

```ini
[Service]
Environment=SPRITE_FORCE_CPU=true
Environment=SPRITE_WARMUP_MODEL=false
```

10:26:14 应用完成启动。已验证 VM `running`、`sprite.service` 活跃、健康检查及前端 HTTP 200、认证后的能力与角色接口 HTTP 200；`/data` 正常挂载，使用 19 GB、可用 276 GB。能力接口报告 `gpu_available=false`，运行环境确认 CPU 强制开关生效。工作室客户端 `192.168.7.143` 已通过 7 网段成功读取角色封面，日志返回 HTTP 200。开发机所在 2 网段直接访问 7 网段仍失败，但可以访问 3 网段服务；未修改跨网段路由。

GPU 尚未恢复。后续应安排显卡及 PCIe 链路检查，必要时计划宿主机断电重启；恢复硬件后再在 VM 停机期间加回上述直通项，并移除临时 CPU 配置、重载 systemd 后重启服务。当前 CPU 模式下图像处理性能下降，GPU 功能不可用。本次未重启宿主机、未修改项目代码或素材数据。

## 内存扩容与 CPU 并发评估

服务恢复后，10:33:48 的 BRIA 抠图触发全局 OOM，Python 当时的匿名 RSS 为 15,968,808 KiB，VM 只有 16 GiB RAM 且没有 Swap。用户随后授权等当前任务结束，再扩容并评估并发。

为保护正在进行的任务，先启用了 4 GiB 的 `/swapfile-sprite-recovery`。当前抠图任务 `f67259560a1e` 于 10:56:06 成功完成 49/49 帧；其间的生成和抽帧任务也都完成。确认没有运行或排队任务后，停止服务并在 Linux 内正常关机，VM 103 内存由 16 GiB 增至 **20 GiB**，CPU 保持 8 核。宿主机总内存约 32 GiB，另两台运行的 VM 共配置 8 GiB，扩容保留了宿主机余量。

4 GiB Swap 已加入 `/etc/fstab`，重启后验证生效。原文件备份在 VM 内 `/etc/fstab.before-sprite-memory-20261016`；扩容前的 VM 配置备份在 PVE `/root/vm103-before-memory-tuning-20261016.conf`。

停业务服务后，用现有 640×640 RGB 帧、同一个 BRIA 模型缓存测试单路与双路。测试进程单独限额 17 GiB、禁用 Swap，避免双路试验耗尽整台 VM 内存。没有覆盖素材，也没有调用付费接口。

| 项目 | 实测结果 |
| --- | --- |
| CPU 模型首次加载 | 117.5 秒，单独统计 |
| 单路连续处理 3 帧 | 70.09 秒；每帧 24.94、23.12、22.02 秒 |
| 单路吞吐 | 2.57 帧/分钟 |
| 单路进程峰值 RSS | 13.77 GiB；先前 49 帧生产任务的服务 cgroup 峰值约 15.16 GiB |
| 双路测试 | 触及 17 GiB cgroup 限额后被测试隔离组终止，没有获得完整吞吐结果；并非 VM 全局 OOM |

最终配置继续使用 CPU，**本地处理并发从 4 降为 1**。`recovery-cpu.conf` 现在包含 `SPRITE_FORCE_CPU=true`、`SPRITE_WARMUP_MODEL=false`、`SPRITE_MAX_WORKERS=1`。这控制本地处理闸门和 cpu 线程池，付费生成的并发参数未修改。GPU 恢复后应重新评估并发，再撤销 CPU 恢复配置。

服务已重新启动，并通过健康检查、前端、认证后的能力及角色接口检查。压测工具为 `scripts/benchmark_cpu_background.py`，机器可读记录保存在 `docs/benchmarks/cpu-background-20261016.json`。当前只是单张帧的有限样本，不能据此推广到其他模型、尺寸或 GPU 模式；结合双路越过内存预算和先前生产 OOM，CPU 本地并发 1 是当前可采用的设置。

## MCP 二期发布候选

用户随后授权继续开发 MCP 二期，并要求线上任务清空后才更新。代码与测试已完成：Windows 262 项通过，Linux 临时目录 260 项通过、2 项因缺参考工程跳过。新增免费加工计划、批次提交/查询/取消，任务 SQLite 持久化、请求编号幂等及网页活动任务去重，以及抠图/导出分池、共享内存预算和动作互斥。暂未上线，生产仍在运行旧任务系统。

发布源文件快照在 `data/releases/mcp-phase2-20261016/release.json`；操作与验收见 `docs/mcp-phase2-release.md`。已创建本对话每 5 分钟检查的 heartbeat 自动化“MCP 二期空闲后上线”（ID `mcp`）：忙时保持静默，空闲且快照匹配后部署，成功验证后暂停自身。不要重复创建自动化、不要使用 --force。发布时还需按操作文档添加资源预算 drop-in，保留当前 CPU 和本地并发 1 设置。

## MCP M3 独立开发（10 月 16 日 15:17）

用户明确要求直接开始 M3 开发，历史“三期规划尚未获授权”的说明已被此新指令取代。
为保留二期发布候选，M3 在本对话附加工作树 `C:\Users\Administrator\.codex\worktrees\mcp-m3\SpriteFrameService`、分支 `codex/mcp-m3` 实现；该目录以原目录的完整二期源码副本为基线，包含未提交的二期实现，不能直接当作相对 HEAD 的纯 M3 补丁合并。

M3 新增 5 个工具（共 19 个）：付费试产/量产计划、审批后提交、生成批次查询、额度用量查询、取消生成。generate 权限之外还需要本地管理员绑定调用方的单次审批和累计调用次数/视频秒数额度；试产成功并人工定型后方可量产。计划及输入哈希冻结、素材与提示词变化重新审批，记账先于供应商调用、未知结果不退款或自动重放，远端视频编号和 usage 保留。管理入口为该工作树中的 `scripts/generation_admin.py`。

验证：Windows 278 项通过，Linux `/tmp/sprite-mcp-m3-qa-final` 276 项通过、2 项缺少参考 Spine 工程跳过；新增 16 项 M3 测试全部使用模拟供应商。未发起真实付费生成，未改变生产配置或重启服务。金额未知时返回 null，实际金额以供应商账单为准；额度仅约束 MCP 入口。

M3 文档为工作树内 `docs/mcp-m3.md`；快照为工作树内 `data/releases/mcp-m3-20261016/release.json`，记录 208 个源文件及相对二期变化的 12 个文件。M3 状态为 tested_not_deployed。原目录二期 204 文件已再次核验完全一致，原自动化继续只处理二期。M3 后续需在二期上线完成后集成其增量、核验基线并按无运行/排队任务的要求安排更新；不能替换二期 release.json 或把当前二期自动化指向 M3。

## MCP 二期已上线（10 月 16 日 15:18）

15:17 空闲检查通过后，核验完整 204 文件快照一致，添加 resource-budget.conf 并用 scripts/deploy_linux.sh --allow-dirty 正常上线。15:18:25 服务 active，备份 `/opt/sprite/.deploy/backups/20261016-151823.tar.gz`。健康、前端、认证后的角色/任务接口 200；MCP 14 工具；SQLite 与 owner 锁已启用；本地并发 1、导出并发 1、预算 18432 MiB、抠图预留 16384 MiB；保持 CPU 强制。原 claude-code 密钥仍 read。启动 warning/error 日志为空，验收只读，未加工素材或调用付费接口。release.json 已标记 deployed；M3 未部署，仍在独立工作树。

## MCP M3 已集成上线（10 月 16 日 15:42）

用户明确授权继续集成上线且仅空闲时更新。主工作目录 D:\AI\SpriteFrameService 已集成独立工作树的 12 个 M3 增量文件，208 个源文件完整核验匹配。Windows 278 项通过；Linux 隔离目录 276 项通过、2 项参考工程缺失跳过。等待线上抠图和导出完成后，认证任务查询为空，脚本暂停准入并复查空闲，以 --allow-dirty 正常更新 10 个运行文件。15:42:09 服务恢复，备份 `/opt/sprite/.deploy/backups/20261016-154207.tar.gz`。

健康、前端、角色与任务接口 200，MCP 19 工具；两份 SQLite quick_check 正常，任务 owner 锁正常。原只读密钥文件完全未变；默认付费额度 0，账本无审批、批次和调用记录。未授予 generate 权限、未提额、未发起真实付费或素材加工。CPU、本地/导出并发及内存预算保留，启动 warning/error 日志无记录。主目录 docs/mcp-m3.md 与 data/releases/mcp-m3-20261016/release.json 为后续权威记录；独立工作树仍保留，不需要再次整树覆盖主目录。原二期自动化仍暂停。

## M3 三项审查修复等待空闲上线（2026-10-19）

用户授权修复导出格式尺寸预算遗漏、已完成抽帧仍检查输入、单文件下载绕过 .incomplete 三项问题，并要求无运行/排队任务才更新。代码修复及 9 个新增参数化回归完成，旧代码 9 项全失败，修复后专项 21 项通过；Windows 全量 287 项通过，Linux 隔离目录 285 项通过、2 项因缺参考 Spine 工程跳过。发布完整 208 文件快照为 data/releases/mcp-m3-fixes-20261019/release.json，相对 M3 仅 6 文件变化（3 个运行文件、CHANGELOG 和 2 个测试文件），尚未部署。生产 83 app 文件核验仍与原 M3 一致。

上线操作与验收见 docs/mcp-m3-fixes-release.md。当前抠图运行中，未重启或修改生产代码。旧自动化 mcp 在应用中已不存在，更新失败后按本次授权新建 heartbeat“M3 修复空闲后上线”，ID m3、每 5 分钟检查。忙时静默、空闲后核验快照并正常部署，验收成功后暂停 m3；禁止 --force 和 --no-restart，不提交真实生成或加工冒烟任务，不新增密钥或额度。

## M3 三项修复已上线（2026-10-19 10:44）

等待抠图结束、认证任务列表 queued/running 清空后，核验 208 文件修复快照和原 M3 生产基线一致。scripts/deploy_linux.sh --allow-dirty 经维护准入及空闲二次检查正常部署 4 个运行文件，10:44:37 服务恢复，备份 `/opt/sprite/.deploy/backups/20261019-104436.tar.gz`。19 个 MCP 工具与各只读接口验收正常；83 个 app 文件哈希吻合，SQLite/owner 锁正常，启动无异常/OOM，权限、额度及资源设置保持不变，未发起真实加工或付费冒烟。release.json 已为 deployed；详情 docs/mcp-m3-fixes-release.md。完成后暂停 m3 自动化，不再重复更新。

## 多角色明确模板绑定已上线（2026-10-19 12:11）

用户授权完善多角色模板绑定并在空闲时上线。新增角色 template_binding：group、first_frame_set_id、shared_template_ids；动作仍以 template_id 显式绑定，跨组视频只接受白名单，首帧不跨组补取。网页、批量入口、流水线、MCP 及付费边界复核权限；导入同 key 多模板不选第一条，角色复制继承绑定但新建默认不复制旧角色首帧。角色看板新增模板绑定/首帧归档界面，生成选择器按角色过滤。

Windows 302 项通过，Linux 300 项通过、2 项缺参考工程跳过，前端构建及隔离界面检查通过。确认线上任务清空后正常 --allow-dirty 部署，12:11:51 服务恢复；备份 `/opt/sprite/.deploy/backups/20261019-121149.tar.gz`。212 源文件快照、84 app 文件与 3 前端构建文件核验；401 生产元数据文件未变。服务接口、19 MCP 工具、SQLite、owner 锁、资源设置及启动日志均正常。快照 data/releases/multirole-bindings-20261019/release.json 标记 deployed，操作说明 docs/multi-role-templates.md。

上线时尚未改绑店长数据；随后用户明确指定店长1为参考来源，配置已完成，见下节。没有创建自动上线任务；原 m3 自动化仍暂停。

## 店长1参考集与两位店长绑定已完成（2026-10-19 12:41）

空闲核验后通过管理员 API 归档店长1的 11 张首帧，参考集 fs_99eee0ec「店长参考集（店长1）」、分组店长。店长1 sp_e0e2f24dee 与店长2 sp_2317ac7173 均固定绑定该集，现有跨组 NPC 正面待机/正面走路/背面待机三个 template_id 显式加入共享白名单，原动作引用未改。逐张归档首帧哈希匹配；角色其他字段、所有动作/素材哈希不变。

店长2只读强制首帧计划 11 项均可运行且指向 fs_99eee0ec；店长1参考选择验证通过，但自身生成因缺已标记正面/背面立绘而阻塞，需要用户补充自身立绘标记。未发起生成或处理，无新增任务、服务无重启。配置备份 /opt/sprite/.deploy/config-backups/shop-binding-20261019T044137Z；验收 data/releases/multirole-bindings-20261019/shop-binding-acceptance.json，release.json 已补配置记录。无需再次上线。

## 店长1图库立绘已标记并验证（2026-10-19 12:44）

用户要求检查现有图片，有可用立绘就直接标记。目视确认 level_1_1（rf_2e7cd100）为正面、level_1_1_back（rf_428854a3）为背面，同角色全身完整；通过管理员 API 标记 front/back。11 动作逐项解析正确（10 正面、1 背面），固定店长参考集 fs_99eee0ec，强制首帧只读计划全部 run，先前缺立绘阻塞已解除。图片哈希与其他图库字段不变，未提交付费或加工任务。备份 /opt/sprite/.deploy/config-backups/shop1-portraits-20261019T044458Z；验收 data/releases/multirole-bindings-20261019/shop1-portraits-acceptance.json。无需重新部署。

## 图文使用手册完成，等待空闲更新（2026-10-19）

应用内手册扩为 18 章，5 图（实际演示界面、店长立绘及关系/工序/资源调度示意），含多角色绑定、立绘标记、排错、MCP 权限与受控生成。离线版 docs/user-manual.html 内嵌全部插图，与 frontend/src/manual.js 同源，由 scripts/export_manual.mjs 导出。构建与隔离浏览器检查通过，5 图均加载；后端未改。

本次线上检查有 7 个 running，未更新。新快照 data/releases/manual-20261019/release.json：220 源文件，11 项手册相关变化；不得再用旧多角色快照部署当前源码。上线步骤与验收见 docs/manual-release.md，仍严格等 queued/running 清空后用正常 --allow-dirty 脚本，禁止强制或跳过重启选项。旧 m3 继续暂停。

图文手册上线自动化 ID automation-2，每 5 分钟检查；忙时静默，成功验收后暂停。隔离预览进程及浏览器已关闭，离线手册无需服务即可打开。

## 图文手册已空闲上线（2026-10-19 17:41）

17:40 确认 queued/running 为空，完整核验 220 源文件和构建快照、84 后端生产基线后，正常 --allow-dirty 更新。备份 /opt/sprite/.deploy/backups/20261019-174101.tar.gz。仅静态前端/README/导出脚本变化，无需重启，服务 PID 19585 不变。健康、前端、认证接口 200，未认证 401，MCP19工具；10 前端文件及7张手册图片的磁盘/HTTP哈希验证通过，后端、角色绑定/立绘标记、密钥及资源配置未变，无生成或加工调用。data/releases/manual-20261019/release.json 状态 deployed，详情 docs/manual-release.md。

手册验收成功后 automation-2 已暂停，旧 m3 保持暂停，不再重复部署。

## 精灵库第一阶段已开发、提交和上线（2026-10-19 20:05）

用户要求先提交既有代码，再实现项目、精灵类型、多级分类、旧数据兼容和测试，空闲时上线。既有工作提交 37d7fdf；第一阶段提交 14a22a8。新增 library_catalog.json（读时不创建）、项目/目录/类型管理员 API、稳定 project_id/sprite_type_id 归属、项目选择与递归分类树、类型默认参考、批量归类预览及过期/冲突/失败保护。原 tags、素材路径、角色绑定和动作不自动改写；旧角色从待归类进入，导入按项目与类型复用同名角色。手册同步到19章6图，详见 docs/library-catalog.md。

Windows327通过，Linux隔离325通过2缺参考工程跳过，Node导航5通过，前端构建与隔离浏览器完整流程通过。确认生产queued/running为空、原基线匹配后，以干净提交正常运行部署脚本，维护准入/二次空闲/备份和重启成功。备份 /opt/sprite/.deploy/backups/20261019-200535.tar.gz；20:05:38恢复，20:06:49验收通过：86 app文件、11前端文件匹配提交和构建，449生产元数据文件未变，空目录只读不落盘，HTTP/认证/MCP19/SQLite/owner锁/资源/启动日志正常，无真实生成加工。发布快照data/releases/catalog-phase1-20261019/release.json为deployed；git_blob_files是干净git archive使用的LF哈希，与测试工作目录仅行尾差异。无需新自动化，旧m3与automation-2继续暂停。

## 房东历史数据归类完成（2026-10-20 09:09）

用户明确所有历史角色均属房东项目、仅租客和店长两类，并要求智能归类。复用已有项目pr_5e35a77df939，新建店长st_519b8850f0e4（2角色）、租客st_3280a5d2a042（14角色，含迁移测试-可删，未删除）。依据店长明确绑定和cash/onwork等专属动作、租客入住/行李动作与NPC引用，16角色均可确定。经管理员API预览+版本复核归类，只改归属，保留全部原标签/绑定/动作配置/立绘标记。店长类型默认用既有fs_99eee0ec绑定；租客类型分组旅馆NPC，未在16张和20张两套首帧间擅自选择。未重启、不改代码、不提交加工，原抠图继续19/49、另一任务仍排队。备份/opt/sprite/.deploy/config-backups/landlord-classification-20261020T010931Z，证据与验收data/releases/landlord-classification-20261020/acceptance.json。

## 待归类空入口隐藏已准备（2026-10-20）

用户要求移除已空待归类。8ba8640仅改前端3文件：零数量隐藏，有遗留或无效项目ID数据时恢复并显示数量；缓存选中已空入口回到全部项目，不受搜索/标签筛选影响。Node7项及构建通过。当前仍有1抠图运行1排队，未部署。快照data/releases/hide-empty-unassigned-20261020/release.json含229源码和11构建文件；操作见docs/hide-empty-unassigned-release.md。已创建5分钟静默heartbeat「待归类空项隐藏空闲上线」，ID automation-3，成功验收后暂停；旧m3与automation-2保持暂停。

用户追加要求4个资源库菜单直接横向显示，已修改共用ResourceMenu和App顶栏布局，去除下拉与点击外部监听；隔离浏览器验证4个入口直接打开原弹窗。已合并到上述release快照（仍229源码11构建，现共5个前端文件变化），原3文件快照失效；automation-3暂停后更新为合并快照并继续等空闲，禁止使用旧哈希误报或重复部署。

## 空待归类隐藏及资源横向入口已上线（2026-10-20 09:41）

09:40确认queued/running为空，完整核验229源文件、11构建文件及86后端基线；显式维护准入后再次确认空闲，正常运行干净部署脚本，完成后恢复准入。仅静态前端变化，无需重启，PID25898不变。备份 `/opt/sprite/.deploy/backups/20261020-094141.tar.gz`，部署提交5ef2edf，功能提交5784b3c（含8ba8640）。09:42验收健康/前端/认证角色任务目录200、未认证401，11构建磁盘/HTTP哈希匹配，86后端与403受保护元数据文件不变；房东店长2/租客14、原参考绑定/立绘/标签、只读权限及资源并发配置均维持，MCP19、SQLite、owner锁与日志正常。未提交加工或付费生成。发布目录data/releases/hide-empty-unassigned-20261020/release.json标记deployed，详见docs/hide-empty-unassigned-release.md及acceptance.json。成功后暂停automation-3，旧m3与automation-2保持暂停。

## 调度死锁已修复并恢复（2026-10-21 09:26）

用户明确授权先核对远端、防止重复付费，再修复调度死锁、补测试并恢复卡住的任务和服务。旧进程自10月20日18:55冻结，13running/1queued。定位ConcurrencyGate在闸门锁内上报进度，反向等待任务锁；准入线程持任务锁查询闸门，形成死锁。修复e88fb23将ctx回调移出闸门锁，补容量再检查/取消释放，JobContext提供job_id供流水线追溯。旧代码在新增回归稳定失败；Windows331通过、Linux隔离329通过2缺参考Spine工程跳过，聚焦40通过。

只查询既有远端ID，5个均已成功；下载到 `/opt/sprite/.deploy/deadlock-recovery-20261021-092110`，验证SHA256和视频解码，备份任务/生成数据库及725份元数据。正常停止旧冻结服务后装回原take，不重新生成。正常部署脚本仅替换2个后端文件，备份 `/opt/sprite/.deploy/backups/20261021-092636.tar.gz`，09:26:37恢复PID36171，无--force或--no-restart。86后端/11静态哈希、部署前后873元数据、接口认证/MCP19/SQLite/owner锁与资源配置验收通过。

10条可确定动作经原API各恢复一次：4个原未提交视频与1个首帧已成功，5条本地处理继续（1抠图+4等待）；抠图从已有6/49帧继续，09:32:55达到12/49，任务查询0.05–0.114秒，无新增错误。原5付费结果ID/哈希未变，22角色归属/模板绑定、密钥、额度和审批账本未变。4条历史强制首帧仅解除残留running、不重放付费流程；1条旧单独generate e5bdaa40d866因缺所属动作和参数保留中断，已询问用户，不猜测重发。详见docs/scheduler-deadlock-recovery.md与data/releases/scheduler-deadlock-20261021（audit、offline-recovery、resume-results、acceptance）。旧任务中断记录保留审计，恢复任务有新ID。无新自动化，不在当前本地处理期间再次部署。

## 监控完成及全局任务管理上线（2026-10-21）

此前恢复的5个本地任务在10:48全部完成并验收抽帧/抠图/所需导出产物，自动化automation-2「本地任务完成与异常监控」已暂停。e5bdaa40d866仍保持中断，用户明确要求不重新提交。

用户随后要求停止后台全部排队/运行任务、保留素材、不重提，再补全局管理入口。11:30暂停新提交并取消2个排队抽帧（57367a6bf3c9、db252f11faa2）和1个运行抠图（89da4c2c800c），待当前推理退出后确认均cancelled、queued/running为0，未强杀服务。备份 `/opt/sprite/.deploy/global-stop-20261021-1129`。

功能提交8e48c01：顶栏全局任务/本页任务栏全局管理，服务端历史查询、角色动作归属/搜索/状态类型过滤/跳转，单项停止、停止全部及新提交暂停/允许。停止全部原子关闭准入、标记全部未完成任务，慢取消回调独立线程且幂等；流水线取消不继续后续工序，保留部分产物；远端ID登记竞态补取消，取消后保留可重挂ID。管理员暂停持久化在scheduler_state.admission_paused，与部署临时维护分离，不自动重放旧任务。普通管理接口仅平台认证可用，原AI只读权限不扩大。Windows339通过、Linux隔离337通过2缺参考工程跳过、Node9通过、构建及隔离浏览器端到端通过，手册同步。

完整233源码/11构建与86生产基线核验、再次空闲确认后正常部署，备份 `/opt/sprite/.deploy/backups/20261021-114939.tar.gz`。11:49:42恢复PID41695，11:51:49验收新老接口/认证/SQLite/owner锁/MCP19/资源配置正常；86后端与11静态哈希匹配，888元数据哈希、66,209素材文件大小/修改时间、410任务载荷哈希保持，0活动任务，无重新提交。2次新增抽帧请求被暂停准入按预期503拒绝，非服务故障；无其他新增错误。当前admission_paused=true，用户可在顶栏「全局任务」手动点「允许新任务」开放新提交，历史取消任务不重做。详见docs/global-task-management.md与data/releases/global-jobs-20261021/release.json（deployed）。无新自动化，隔离预览已关闭。
