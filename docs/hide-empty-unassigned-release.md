# 待归类空项自动隐藏（2026-10-20）

用户要求移除已空的待归类入口。代码提交 8ba8640：按完整库存隐藏空入口，未归类/项目ID不可解析的数据再次出现时恢复并显示数量；缓存中选中已空入口时回到全部项目。不修改后端或生产数据。Node7项通过，Vite构建通过，现有导入恢复兼容性保留。线上仍有抠图运行与排队，本轮未更新。

发布快照 data/releases/hide-empty-unassigned-20261020/release.json（229源文件，11构建文件）。用户追加要求将4个资源入口横向展示，已合并重新验证快照：相对第一阶段共 libraryModel.js、SpriteLibraryView.vue、分类导航测试、App.vue、ResourceMenu.vue 5文件变化。四个入口为参考视频库、参考首帧库、提示词库、项目包导出/导入，精灵库/看板/工作台共用；取消下拉，窄宽度允许横向按钮换行。隔离浏览器逐一验证4个原弹窗可直接打开，截图data/releases/hide-empty-unassigned-20261020/navigation-preview.png，构建通过。以release.json当前commit和hash为准，旧3文件快照已作废。

1. 每轮先只读经SSH ProxyJump root@192.168.3.12到ubuntu@192.168.3.225；在/opt/sprite/backend用get_settings().auth_token认证查询http://127.0.0.1:8000/api/jobs?limit=200，不打印令牌、不初始化第二个JobManager。queued/running非空立即静默结束，不修改服务器、不取消任务；查询失败不当空闲。
2. 空闲后按快照scopes用git ls-files -z -c -o --exclude-standard --核验完整源文件集合及SHA256；核验frontend/dist完整集合及dist_files哈希。意外变化不部署，通知需重新验证。
3. 发布目录check_remote.py再次只读检查空闲、86后端基线，返回生产元数据哈希及already_deployed。保存返回值为本次production-before.json。若already_deployed=true，只验收补记录，不重复部署。
4. 使用Windows Git Bash执行scripts/deploy_linux.sh（代码已提交且部署源范围应干净），不使用--force或--no-restart。脚本负责维护准入、二次空闲检查、备份和回滚；新任务导致拒绝时等下轮。不要改动运行/排队任务。
5. 验收service active、健康/前端/认证角色任务目录200、未认证401；全部dist_files生产磁盘/HTTP哈希匹配，86后端基线不变，本轮更新前metadata全部不变。房东16角色归属、原绑定/立绘/标签、密钥权限、CPU/并发/内存资源维持，不发真实加工或生成测试。无需在生产制造未归类角色，显示恢复及缓存回退已由隔离测试覆盖。
6. 记录备份、时间和验收到本文件、docs/codex-handoff.md及本次release.json（status=deployed），成功后暂停本轮heartbeat自动化并通知用户。旧m3和automation-2继续暂停，不归档对话。忙碌与未变化静默，仅成功、真实失败或需要用户处理时通知。

自动化ID automation-3，名称「待归类空项隐藏空闲上线」，每5分钟检查，成功后暂停。

## 已上线（2026-10-20 09:41）

09:40任务检查为空，229源文件与11构建文件完整集合及SHA256均匹配快照，86后端基线匹配。正常执行干净部署脚本，实际部署提交5ef2edf（仅增加发布文档，功能提交5784b3c）。备份 `/opt/sprite/.deploy/backups/20261020-094141.tar.gz`。本次只有静态前端变化，无需重启，PID25898保持不变。由于现有脚本只在需要重启时启用维护准入，本轮在调用脚本前显式暂停准入、再次核验空闲，完成后在finally恢复准入，未取消或打断任务。

09:42验收通过：健康、前端、认证角色/任务/目录接口200，未认证401；11构建文件磁盘及HTTP内容SHA256匹配，86后端及403受保护元数据文件不变。房东16角色仍为店长2、租客14；原绑定、参考素材、立绘、标签和只读密钥权限不变。MCP19工具、SQLite完整性、owner锁、CPU及并发/内存配置正常，检查期间无新增错误日志，无生产加工或付费测试。证据为本发布目录production-before.json、deploy.log、acceptance.json；release.json标记deployed。成功后暂停automation-3，旧m3与automation-2保持暂停。
