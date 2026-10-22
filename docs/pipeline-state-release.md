# 流水线停止后的状态与重复提交修复（2026-10-22）

用户反馈停止后台任务后，一键流水线仍全部提示“流水线进行中”，单步操作却可执行。根因是 start/resume 只判断 action.json 的 pipeline.status；进程异常退出、尚未开始运行就取消的任务未必能清理这个标记。排队中的旧版流水线还没有保存 job_id。

## 本次修复与验证

- 以当前 JobManager 中未真正退出的 pipeline 为准，按精灵和动作同时匹配；不受历史分页限制。正在排队、运行或取消退出中都阻止重复提交；已中断/取消的旧记录不再永久阻塞。
- start/resume 共用短期提交锁，排队时立即保存 job_id，工作线程等提交完成才读取配置，避免同时点击产生重复任务或快任务完成状态被 queued 覆盖。准入拒绝时恢复原流水线记录。
- 看板与计划只读校正陈旧状态，显示已停止/停止中；不改 action.json，不自动执行任务，不删除已有产物。续跑保留完成步骤、原参数和资产检查，未强制重做的现有产物继续跳过。
- 重复动作 ID 去重。全部步骤完成时显式 Spine 导出保留；因为真正运行/阻塞而跳过时不另起 Spine 导出。
- 新增20项回归，包含持久化重启、停止后 start/resume、丢失 job_id 的真实排队、退出中保护、8路同时提交、准入拒绝、快任务状态、只读看板、批次成员和 Spine 分支。

Windows完整381项通过；Linux隔离379通过、2项缺参考Spine工程跳过。前端构建通过，仅有原有重复静态/动态导入提示。Windows首次全测使用D盘临时目录触发旧迁移测试的“路径不含D盘”断言，改用系统临时目录后完整通过；没有改动迁移实现或测试规避问题。

## 当前线上与发布约束

截图时间09:06；09:36已经按此前授权恢复33条免费流水线。09:51只读检查：2完成、31未完成（16运行含资源等待，15排队），店长6/sleep完成，exclusivewait正在补图21/28，hawk/clean/onwork真实等待处理名额。因此目前提示进行中的这些任务不应再次提交。不能为处理历史截图而重启、取消或重放它们。

发布快照位于 `data/releases/pipeline-state-20261022/release.json`。当前状态为等待空闲，尚未上线本次状态修复。现有内存修复保持生效。缺参数e5bdaa40d866以及此前DNS生成失败均不重提，不新增加工或付费测试。

## 空闲发布步骤

1. 每轮先运行 `.venv/Scripts/python.exe data/releases/pipeline-state-20261022/run_check.py`。只读通过现有SSH密钥及ProxyJump root@192.168.3.12连接ubuntu@192.168.3.225；认证仅从远端/opt/sprite/backend的get_settings().auth_token读取，不打印密钥，不初始化第二个JobManager。queued/running或control.active非零即返回idle=false，静默结束，不变更服务器。查询失败绝不是空闲。
2. 空闲时运行同目录 `verify_local.py`，按快照scopes用git ls-files -z -c -o --exclude-standard --核验完整源码集合及SHA256，检查frontend/dist完整集合及dist_files。必须clean且HEAD匹配快照commit；意外变化停止并通知重新验证。
3. run_check.py在空闲时同时核验生产86后端完整基线、静态基线并保存production-before.json（素材文件大小/mtime、精灵及配置JSON哈希、SQLite任务载荷哈希、资源配置）。生产若已匹配本次expected与dist，返回already_deployed=true，只验收补记录，不重复部署。若非旧基线也非本次版本则停止。
4. Windows Git Bash执行 `scripts/deploy_linux.sh`，禁止--force、--no-restart或绕过维护准入。脚本再次关闭准入并检查空闲，备份并失败回滚；如果新任务进入导致拒绝，等待下一轮，不能取消新任务。无需变更GPU、内存、并发、权限、额度、审批和管理员接收状态。
5. 部署后运行 `.venv/Scripts/python.exe data/releases/pipeline-state-20261022/run_check.py verify`。验收service active、健康/前端/认证角色目录任务接口200、未认证401、MCP19工具、86后端及完整静态磁盘/HTTP哈希、SQLite完整性及owner锁、原资源设置和所有before素材/配置/任务记录保持。只读检查动作看板状态与实际任务一致，不提交生产流水线来验证。
6. 在本文件、docs/codex-handoff.md与release.json记录时间、备份、结果，status=deployed。成功后暂停本次自动化并通知用户；忙碌或无变化静默，仅成功、真实失败或需要用户处理时通知。不归档对话、不启用旧自动化。
