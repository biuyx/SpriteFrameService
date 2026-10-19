# 图文手册更新与空闲发布（2026-10-19）

用户要求更新使用手册、图文并茂，并沿用没有运行或排队任务才更新上线的约束。

## 已完成

- 应用内手册 18 章：新增角色绑定、固定参考集、共享参考、立绘标记、MCP 权限与受控生成，补齐内存排队及下载半成品说明。
- 5 张插图：角色参考关系、完整流程、隔离演示的真实绑定窗口、店长1实际正反面立绘、资源调度。
- 手册图片支持点击放大与替代文字，保留现有章节搜索与工序定位。
- `node scripts/export_manual.mjs` 从同一内容生成 `docs/user-manual.html`。5 图全部内嵌，可离线打开与浏览器打印。
- 前端构建通过（仅原有 stores 静态/动态导入警告）；隔离浏览器验证窗口显示、立绘 SVG 和离线版全部 5 图加载正常。未更改后端，不需要重复跑业务测试。

## 待发布

本次检查线上有 7 个 running 任务，未部署，未重启、取消或修改生产任务。
快照 `data/releases/manual-20261019/release.json`：220 源文件，11 项相对多角色版本新增或变更。仅 README、手册内容/渲染组件、插图和离线导出脚本；后端完全不变。

1. 使用 SSH ProxyJump root@192.168.3.12 到 ubuntu@192.168.3.225，只读查询认证的 `/api/jobs?limit=200`。远端在 /opt/sprite/backend 用 get_settings().auth_token 认证，不打印令牌，不初始化第二个 JobManager。queued/running 非空时静默结束本轮；查询失败不能当空闲。
2. 空闲后以 release.json 的 scopes 用 `git ls-files -z -c -o --exclude-standard --` 核验完整文件集合与 SHA256，另外核验 dist_files 与本地 frontend/dist 完整文件集合及 SHA256。不一致不部署，需要重新验证。
3. `data/releases/manual-20261019/check_remote.py` 只读复核空闲及原多角色版本的 84 个后端 app 文件。若线上已是本次 dist_files 对应版本，直接验收补记录，禁止重复部署。
4. 在项目根目录运行 `& 'C:\Program Files\Git\bin\bash.exe' scripts/deploy_linux.sh --allow-dirty`，不用 --force 或 --no-restart。脚本负责维护准入、空闲复查、备份及失败回滚。新任务进入导致拒绝时等待下一轮。
5. 验收 service active，健康、前端、认证后的角色/任务接口 200；dist_files 全部生产文件哈希一致，`/manual/` 下 7 个插图/原图均 HTTP 200，JS/CSS 可取回；后端 84 文件与原基线不变，店长1正背面标记与两角色 template_binding 不变，MCP 19 工具、只读密钥权限及资源配置保持。只读冒烟，不提交生成或加工。
6. 记录上线时间、备份和验收到本文件、docs/codex-handoff.md 与本次 release.json（status=deployed）。成功后暂停本次图文手册自动化并通知用户。不要更改旧 m3 自动化，也不要归档对话。

只在成功、真实失败或需用户处理时通知；线上持续忙碌时保持静默。

已创建每 5 分钟静默检查的 heartbeat，ID automation-2，名称「图文手册空闲后上线」；完成后暂停此 ID。

## 已上线（2026-10-19 17:41）

17:40 认证任务查询 queued/running 清空；220 源文件及 10 个完整构建文件与已验证快照一致，84 后端 app 文件生产基线吻合，生产尚未安装新版手册。正常执行部署脚本 --allow-dirty，经维护准入及空闲复查完成更新。备份 `/opt/sprite/.deploy/backups/20261019-174101.tar.gz`。仅前端、README 和离线导出脚本变化，脚本判断无需重启，服务 PID 19585 与启动时间均保持不变；未使用强制或跳过重启选项。

17:41:31 验收：健康、前端、认证角色/任务/认证状态接口 200，未认证受保护接口 401；全部 10 个前端文件生产哈希匹配，7 张手册图片及 JS/CSS 均 HTTP 200 且响应内容哈希匹配。MCP 19 工具，84 后端 app 文件未变；两位店长模板绑定、店长1正背面立绘标记、密钥记录与权限、CPU/并发/内存配置均与更新前一致。未提交生成或加工测试。验收记录 `data/releases/manual-20261019/acceptance.json`，本次 release.json 已标记 deployed。

验收完成后已暂停本次 automation-2 自动化，防止重复部署；旧 m3 仍保持暂停。
