# 接入火山方舟 Ark 视频生成 —— 设计方案

目标：把「生成视频」并入服务，闭环成 **生成 → 抽帧 → 抠图 → 找循环 → 导出精灵图**。
放弃原 SpriteFrameStudio 的 i2v / SmoothMix 实现，改接 Ark。

---

## 0. 接口事实（已核实 / 待实测）

**已核实**

| 项 | 值 |
| ---- | ---- |
| Base URL | `https://ark.cn-beijing.volces.com/api/v3` |
| 鉴权 | `Authorization: Bearer <API_KEY>` |
| 创建任务 | `POST /contents/generations/tasks` |
| 查询任务 | `GET /contents/generations/tasks/{id}` |
| 任务状态 | `queued` / `running` / `succeeded` / `failed` |
| 结果字段 | `content.video_url`（**带签名的临时链接，必须立即下载转存**） |
| 参数传递 | 顶层 JSON 字段，或写在 prompt 末尾的 `--resolution 1080p --ratio 16:9 --duration 5` 后缀，二者等效 |

请求体（图生视频）：

```json
{
  "model": "doubao-seedance-1-0-pro-250528",
  "content": [
    {"type": "text", "text": "提示词 --resolution 720p --ratio 1:1 --duration 5"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}, "role": "first_frame"}
  ]
}
```

响应：`{"id": "cgt-2026xxxx-xxxxxx"}`

**待实测**（首次联调时确认，不要写死）

- 取消任务是否为 `DELETE /contents/generations/tasks/{id}`
- `image_url` 是否接受 `data:` base64（若只收公网 URL，需改走「先传对象存储」）
- 各模型对 duration / resolution / ratio 的合法取值组合
- 单次请求体大小上限（决定首帧图要压到多大）
- 实际可用的 model id 列表与计费单价

> `callback_url` 参数不采用：本服务通常跑在内网 / 本机，没有公网回调地址，只能轮询。

---

## 1. 存储模型：视频从「单个」改成「多版本 take」

**为什么必须改**：现有 `save_video()` / `save_video_stream()` 开头都调 `_clear_videos()`，
一个会话只能存一个视频。而生成是抽卡式的——生成三版挑一版是常态，现在每生成一次
就把上一版删了。

新布局：

```
data/sessions/{sid}/video/
    takes.json            # {"current": "t_ab12", "takes": [ {...}, ... ]}
    t_ab12.mp4            # 视频本体
    t_ab12.jpg            # 首帧封面（列表缩略图用）
```

`takes.json` 中每条：

```json
{
  "id": "t_ab12",
  "source": "generate",            // generate | upload
  "created_at": 1786400000.0,
  "status": "succeeded",           // pending | running | succeeded | failed
  "remote_task_id": "cgt-xxx",     // 生成任务才有，用于重启后重挂
  "model": "doubao-seedance-1-0-pro-250528",
  "prompt": "...",
  "params": {"resolution": "720p", "ratio": "1:1", "duration": 5, "seed": 123},
  "first_frame": {"kind": "frame", "index": 7},   // 或 {"kind":"upload","name":"a.png"}
  "error": null
}
```

**兼容性关键**：`SessionStorage.video_path` 改为「返回当前选中 take 的路径」。
这样抽帧、视频预览、`_restore()` 等所有下游代码**一行都不用改**，上传视频也
自然变成「新增一个 source=upload 的 take」。

---

## 2. 任务执行：生成任务不能走现有通道

两个现实约束：

1. **会话锁**：上一轮给所有 job 加了 `lock=session.lock` 串行执行，防止并发改帧数据。
   但生成任务是几分钟的纯网络等待，且只往 `video/` 写新文件、不碰帧数据，**没有竞态**。
   让它占着会话锁几分钟，会把同会话的抠图、导出全部堵住。
2. **线程池只有 2 个 worker**：一个生成任务会占着线程干等数分钟，两个并发就把池占满，
   所有本地处理停摆。

方案：`JobManager` 增加第二个线程池，按用途分流。

```python
class JobManager:
    def __init__(self, max_workers=None, io_workers=8):
        self._executor    = ThreadPoolExecutor(max_workers or settings.max_workers)  # CPU
        self._io_executor = ThreadPoolExecutor(io_workers)                           # 外部 API 等待

    def submit(self, job_type, fn, lock=None, pool="cpu"): ...
```

生成任务用 `job_manager.submit("generate", _job, pool="io")`，**不传 lock**。
其余任务行为完全不变。

---

## 3. 远端任务持久化：这次是花钱的

本地任务丢了重跑即可；**远端生成任务丢了，那边还在跑、还在计费，结果却拿不回来**。

- 拿到 `remote_task_id` 的第一时间就写进 `takes.json`（status=`running`），再开始轮询
- 进程重启后，`SessionManager._restore()` 里已有的恢复逻辑之外，增加：
  把 status 为 `pending`/`running` 的 take 标为「需要重挂」
- 新增 `POST /api/sessions/{sid}/takes/reconcile`：对这些 take 重新 `GET` 远端任务，
  成功则下载视频落盘、置 `succeeded`；失败则置 `failed`
- 前端进入「视频生成」页时自动调一次 reconcile

轮询策略：首次 5s，之后指数退避到最长 15s，总超时 15 分钟；`ctx.report()` 上报进度，
`ctx.register_cancel()` 绑定远端取消（待实测端点）。

**视频 URL 是临时签名链接**，`succeeded` 后必须立即下载转存到 `t_xxx.mp4`，
不能只存 URL。

---

## 4. 首帧图：直接复用会话里的帧

图生视频的首帧有两个来源：

- `{"kind": "upload", ...}` —— 用户上传一张图
- `{"kind": "frame", "index": N}` —— **直接取当前会话的第 N 帧**

第二种是这个功能真正的价值点：抠完图、挑好一帧，直接拿它当首帧继续生成下一段动作，
不用导出再上传。

编码：读取帧 → 等比缩到最长边 ≤1024 → JPEG(q=90) → `data:image/jpeg;base64,...`。
缩放是必须的，原图直接 base64 很容易超请求体上限。

---

## 5. 配置与密钥

```ini
# 火山方舟视频生成（留空则该功能在前端置灰）
SPRITE_ARK_API_KEY=
SPRITE_ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
SPRITE_ARK_MODEL=doubao-seedance-1-0-pro-250528
SPRITE_ARK_TIMEOUT_SECONDS=900

# 配额：服务已可对内网开放，共享 key 需要兜底
SPRITE_GENERATE_DAILY_LIMIT=20      # 每日最多提交次数，0 = 不限制
SPRITE_GENERATE_MAX_CONCURRENT=2    # 同时进行中的生成任务数上限
```

铁律：

- **key 绝不出现在 `/api/capabilities` 等探测接口的返回里**，只暴露 `"configured": true/false`
- **key 绝不进日志**，异常信息里也要过滤
- 绿色包是带 `.env` 分发的——**打包脚本必须排除 key**，说明书里让使用者自己填

---

## 6. API 设计

| 端点 | 说明 |
| ---- | ---- |
| `GET /api/generate/capabilities` | 是否已配置 key、模型列表、参数取值范围、今日剩余配额 |
| `POST /api/sessions/{sid}/generate` | 提交生成 → `{job_id, take_id}` |
| `GET /api/sessions/{sid}/takes` | 版本列表（含封面 URL、参数、状态） |
| `GET /api/sessions/{sid}/takes/{tid}/cover` | 封面图 |
| `POST /api/sessions/{sid}/takes/{tid}/select` | 切为当前视频（之后抽帧即针对它） |
| `DELETE /api/sessions/{sid}/takes/{tid}` | 删除某版本 |
| `POST /api/sessions/{sid}/takes/reconcile` | 重启后重挂未完成的远端任务 |

请求体：

```json
{
  "mode": "i2v",
  "prompt": "角色向前走，循环动作",
  "first_frame": {"kind": "frame", "index": 7},
  "params": {"resolution": "720p", "ratio": "1:1", "duration": 5, "seed": 42}
}
```

---

## 7. 前端

侧栏在「视频抽帧」**之前**加一项「视频生成」（它是流程起点）：

- 未配置 key 时整页置灰，提示去 `.env` 填 `SPRITE_ARK_API_KEY`
- 表单：模式(文生/图生) / 提示词 / 首帧来源(上传 or 选当前帧) / 分辨率 / 比例 / 时长 / seed
- 提交后走现有 `startJob` 轮询机制，进度条复用 JobPanel
- 下方版本列表：封面 + 参数 + 状态，点「用这个」即 select 并跳到「视频抽帧」页
- 生成中显示「预计耗时数分钟，可切到其他页面继续工作」

---

## 8. 改动清单

**新增**

- `backend/app/core/ark_client.py` —— Ark HTTP 客户端（创建/查询/取消/下载）
- `backend/app/core/video_generator.py` —— 生成编排：参数拼装、轮询、落盘、写 takes.json
- `backend/app/services/take_store.py` —— takes.json 读写与当前版本管理
- `backend/app/api/generate.py` —— 上述端点
- `frontend/src/views/GenerateView.vue`

**修改**

- `services/storage.py` —— `video_path` 改为读当前 take；上传走 take 化
- `services/job_manager.py` —— 增加 io 池与 `pool=` 参数
- `services/session.py` —— `_restore()` 标记待重挂的 take
- `api/router.py` / `App.vue` / `nav.js` —— 各加一行
- `config.py` / `.env.example` —— Ark 配置项
- `backend/requirements.txt` + `scripts/build_portable.ps1` —— **httpx 从 dev 移入运行时**
  （目前运行时没有任何 HTTP 客户端，绿色包也没打）

**不动**：抽帧、抠图、分析、导出、历史——全部通过 `video_path` 的兼容改法自然衔接。

---

## 9. 风险与待定

| 项 | 说明 |
| ---- | ---- |
| **计费模型（需你拍板）** | 服务已开内网。共享一个 key + 每日配额，还是每人填自己的 key？前者简单但额度是公共的，后者需要把 key 放进会话/用户维度，改动更大。建议先做共享 key + 配额 + 二次确认。 |
| 临时链接过期 | `video_url` 是签名链接，必须在回调到 `succeeded` 后立刻下载；下载失败要重试并保留 `remote_task_id` 以便重试 |
| 取消端点未验证 | 若 Ark 不支持取消，前端「取消」只能停止本地轮询，远端仍会跑完并计费——必须在 UI 上如实说明 |
| 磁盘 | 多版本 take 会累积视频文件，需要「只保留最近 N 个」或手动清理入口 |
| 抽卡成本 | 生成前弹一次确认，展示预计消耗；配额用尽时明确报错而不是静默失败 |

---

## 10. 建议实施顺序

1. **take 存储模型**（含 `video_path` 兼容改法）——地基，且不依赖 Ark，可独立验证
2. **JobManager 分池**——小改动，解开长任务堵塞
3. **ark_client + 联调**——先用脚本打通创建/查询/下载，实测第 0 节的待验证项
4. **generate 端点 + 持久化重挂**
5. **前端页面**
6. **打包脚本补 httpx、说明书补配置**

1、2 步可以先做，与 Ark 无关，做完就能独立回归测试。
