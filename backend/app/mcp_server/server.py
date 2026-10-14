"""MCP 服务：让 Claude Code 等 AI 客户端查询、审核平台上的生产进度。

挂在 /api/mcp（Streamable HTTP，无状态 + JSON 响应）：
  · 无状态：每次请求自成一体，服务重启、多进程都不怕会话丢失
  · 挂在 /api 下：经过平台的认证中间件，只认 Bearer（平台口令或 AI 密钥）

一期只开放只读工具。工具按「先定型、再量产」的思路组织，见 INSTRUCTIONS。
"""
from __future__ import annotations

import contextlib
import functools
import json
import time
from typing import Annotated, Any, List, Literal, Optional

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse

from app.mcp_server import audit, queries, views
from app.security import agent_of
from app.services.sprite_store import sprite_store

INSTRUCTIONS = """\
帧动画生成平台（SpriteFrameService）。一个「角色」下有一批「动作」（待机、走路……），
每个动作走一条流水线：首帧 → 视频生成 → 抽帧 → 抠图 → 导出。

工作理念：先定型，再量产。
- 定型：在样板角色上把「流水线模板」调稳——参考视频模板（动作节奏、时长、抽帧规则）、
  提示词库（按 模板 > 动作key > 分组 > 全局 解析，有版本）、参考首帧集、角色工艺预设
  （抠图参数、缩放描边、导出格式）。get_template_library 可以看全部定型资产。
- 量产：新角色直接套这些设定批量跑，每个动作的设定由模板解析出来，不临时填写。
- 量产里出了问题分两类：个别动作没生成好 → 只重做这一个；多个角色的同一个动作都有问题
  → 是模板的问题，停下来报告给人去改模板，不要逐个改提示词绕过去。

现在开放的都是只读工具，建议的用法：
1. list_sprites / get_sprite_overview：看每个角色、每个动作卡在哪一步；
2. 量产前 check_readiness：每个动作每一步会执行 / 跳过 / 被什么阻塞，设定来自哪一层哪个
   版本，要花几次生成。阻塞要先补齐；「内置兜底」「动作单独设定」的提示词说明没定型或偏离了模板；
3. 看图审核：view_first_frames（首帧和立绘像不像，编号 1/2 是立绘）、view_frames（帧序列是否
   连贯、抠图边缘是否干净，透明处是棋盘格）、view_video（生成的视频抽样）；
4. 长任务用 wait_job 等，没完成就再调一次。任务记录在内存里，服务重启会丢，丢了先看动作进度，
   不要重复提交。

角色和动作的参数都可以传 id 或名字。"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# 每个工具要求的权限（AI 密钥的 scopes 里得有）
TOOL_SCOPES = {}


def _scope(name: str, scope: str = "read"):
    TOOL_SCOPES[name] = scope
    return name


def _caller(ctx) -> dict:
    req = getattr(ctx, "request", None)
    if req is None:                     # 进程内直连（测试）：没有 HTTP 层
        return {"id": "local", "name": "进程内", "scopes": ["read", "operate", "generate"]}
    return agent_of(req.headers) or {"id": "?", "name": "未认证", "scopes": []}


def _error(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], isError=True)


def _is_error(result: Any) -> tuple:
    if isinstance(result, CallToolResult):
        err = bool(result.is_error)
        text = next((c.text for c in result.content if getattr(c, "type", "") == "text"), "")
        return err, text
    if isinstance(result, dict):
        err = bool(result.get("isError"))
        text = next((c.get("text", "") for c in result.get("content") or []
                     if isinstance(c, dict) and c.get("type") == "text"), "")
        return err, text
    return False, ""


class GuardAndAudit:
    """工具调用的统一关口：按密钥权限放行，每次调用都记审计。"""

    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params or {}
        tool = params.get("name", "?")
        args = params.get("arguments") or {}
        agent = _caller(ctx)
        need = TOOL_SCOPES.get(tool, "read")
        t0 = time.perf_counter()
        if need not in (agent.get("scopes") or []):
            msg = (f"这把密钥（{agent.get('name')}）没有「{need}」权限，不能调用 {tool}。"
                   f"请管理员用 scripts/agent_keys.py 发一把带该权限的密钥")
            audit.record(agent, tool, args, False, 0, msg)
            return _error(msg)
        try:
            result = await call_next(ctx)
        except Exception as e:
            audit.record(agent, tool, args, False, (time.perf_counter() - t0) * 1000, str(e))
            raise
        err, text = _is_error(result)
        audit.record(agent, tool, args, not err, (time.perf_counter() - t0) * 1000,
                     text if err else "")
        return result


server = MCPServer(name="sprite-frame", title="帧动画生成平台",
                   description="查询与审核帧动画生产进度", instructions=INSTRUCTIONS,
                   version="1", middleware=[GuardAndAudit()])


async def _run(fn, *args, **kwargs):
    """存储层是同步读盘的，放线程里跑，别卡住事件循环。"""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def _wrap_errors(fn):
    """参数错误转成给 AI 看的提示（SDK 会把异常变成 isError 结果）。"""
    @functools.wraps(fn)
    async def inner(*a, **kw):
        try:
            return await fn(*a, **kw)
        except queries.ToolInputError as e:
            return _error(str(e))
    return inner


SpriteRef = Annotated[str, Field(description="角色 id 或名字")]
ActionRef = Annotated[str, Field(description="动作 id 或名字")]


# ---------------------------------------------------------------- 了解现状
@server.tool(name=_scope("list_sprites"), title="角色列表", annotations=READ)
@_wrap_errors
async def list_sprites(
        keyword: Annotated[str, Field(description="按名字或标签筛选，留空列出全部")] = "",
) -> dict:
    """列出角色，以及各自的动作分别卡在哪一步（缺首帧 / 缺视频 / 待抽帧 / 待抠图 / 待导出 / 已导出 / 已定稿）。"""
    return await _run(queries.list_sprites, keyword)


@server.tool(name=_scope("get_sprite_overview"), title="角色总览", annotations=READ)
@_wrap_errors
async def get_sprite_overview(sprite: SpriteRef) -> dict:
    """一个角色的全貌：立绘是否标记、工艺预设（抠图参数、导出时的缩放描边、导出格式），
    每个动作的阶段、绑定的参考视频模板、视频版本数、帧数、已抠图数、导出数、流水线状态。"""
    return await _run(queries.sprite_overview, sprite)


@server.tool(name=_scope("get_template_library"), title="定型资产", annotations=READ)
@_wrap_errors
async def get_template_library(
        kind: Annotated[Literal["all", "reference_videos", "prompts", "first_frame_sets",
                                "spine_templates"],
                        Field(description="只看某一类；默认全部")] = "all",
) -> dict:
    """流水线模板的全部定型资产：参考视频模板（时长、抽帧规则、绑定了多少动作）、提示词库
    （作用域、绑定层级、当前版本）、参考首帧集（覆盖哪些动作 key）、Spine 导出模板。"""
    return await _run(queries.template_library, kind)


@server.tool(name=_scope("get_platform_status"), title="平台状态", annotations=READ)
@_wrap_errors
async def get_platform_status() -> dict:
    """GPU、已安装的抠图模型、视频生成与 OSS 是否配置、生成并发上限、进行中的任务数。"""
    return await _run(queries.platform_status)


# ---------------------------------------------------------------- 量产前体检
@server.tool(name=_scope("check_readiness"), title="就绪检查", annotations=READ)
@_wrap_errors
async def check_readiness(
        sprite: SpriteRef,
        actions: Annotated[Optional[List[str]], Field(
            description="只查这些动作（id 或名字）；不给就是全部")] = None,
        steps: Annotated[Optional[List[Literal["firstframe", "generate", "extract",
                                               "matting", "export"]]], Field(
            description="只查这些步骤；不给就是整条流水线")] = None,
) -> dict:
    """量产前的体检。对每个动作的每一步给出 执行 / 跳过（已完成）/ 阻塞（原因），
    列出设定来源（参考视频模板、首帧和视频提示词命中哪一层哪个版本、视频时长从哪来），
    汇总阻塞原因、没定型的提示词，以及要花的生成次数（首帧生图几张、视频几段共几秒）。
    只做检查，不会启动任何任务。"""
    return await _run(queries.readiness, sprite, actions, steps)


# ---------------------------------------------------------------- 看图审核
def _first_frames_sheet(sprite_ref: str, action_refs: Optional[List[str]]):
    sp = queries.find_sprite(sprite_ref)
    sid = sp["id"]
    acts = queries.find_actions(sp, action_refs)
    if len(acts) > 40:
        raise queries.ToolInputError(f"一次最多看 40 个动作，这个角色有 {len(acts)} 个——用 actions 分批看")
    imgs, legend = [], []
    for role, label in (("front", "正面立绘"), ("back", "背面立绘")):
        ref = queries._portraits(sid)[role]
        if ref:
            imgs.append(views.load(sprite_store.ref_path(sid, ref)))
            legend.append(f"{len(imgs)} = {label}")
    missing = []
    for a in acts:
        p = sprite_store.action_dir(sid, a["id"]) / "first_frame.png"
        imgs.append(views.load(p) if p.is_file() else None)
        name = a.get("name") or a["id"]
        legend.append(f"{len(imgs)} = {name}" + ("（没有首帧）" if not p.is_file() else ""))
        if not p.is_file():
            missing.append(name)
    head = f"角色「{sp.get('name')}」的首帧，共 {len(acts)} 个动作"
    if missing:
        head += f"，{len(missing)} 个还没有首帧"
    return views.contact_sheet(imgs), head + "。编号对照：\n" + "\n".join(legend)


@server.tool(name=_scope("view_first_frames"), title="看首帧", annotations=READ)
@_wrap_errors
async def view_first_frames(
        sprite: SpriteRef,
        actions: Annotated[Optional[List[str]], Field(
            description="只看这些动作；不给就是全部（最多 40 个）")] = None,
) -> list:
    """把角色的立绘和各动作首帧拼成一张编号总览图，用来检查首帧和立绘是否是同一个角色
    （脸、发型、服装、配色、比例）、姿势是否对应动作。图后附编号对照。"""
    jpeg, text = await _run(_first_frames_sheet, sprite, actions)
    return [Image(data=jpeg, format="jpeg"), text]


def _frames_sheet(sprite_ref: str, action_ref: str, kind: str, max_frames: int):
    from app.services.frame_store import resolve_frame_file
    sp = queries.find_sprite(sprite_ref)
    a = queries.find_actions(sp, [action_ref])[0]
    d = sprite_store.action_dir(sp["id"], a["id"])
    try:
        frames = json.loads((d / "frames.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        frames = []
    frames = sorted((f for f in frames if f.get("id")), key=lambda f: f.get("index", 0))
    if not frames:
        raise queries.ToolInputError(f"动作「{a.get('name')}」还没有抽帧")
    processed = sum(1 for f in frames if f.get("processed_path"))
    sub = "proc" if kind == "processed" else "raw"

    def path_of(f):
        stored = f.get("processed_path") if kind == "processed" else f.get("image_path")
        return resolve_frame_file(stored, d / "frames" / sub / f"{f['id']}.png")

    pick = views.pick_evenly(frames, max(1, min(max_frames, 48)))
    imgs, legend = [], []
    for f in pick:
        p = path_of(f)
        imgs.append(views.load(p) if p and p.is_file() else None)
        legend.append(f"{len(imgs)}=第{f.get('index')}帧")
    if kind == "processed":
        imgs = views.crop_union(imgs)
    notes = []
    if kind == "processed" and processed < len(frames):
        notes.append(f"{len(frames) - processed} 帧还没抠图，格子里是占位")
    sizes = views.size_census(p for p in map(path_of, frames) if p and p.is_file())
    if len(sizes) > 1:
        notes.append("帧尺寸不一致（" + "、".join(f"{k} ×{v}" for k, v in sizes.items())
                     + "），导出的序列帧会大小不一，多半是部分帧被单独缩放过")
    head = (f"「{sp.get('name')} / {a.get('name')}」共 {len(frames)} 帧，已抠图 {processed} 帧；"
            f"这里均匀取了 {len(pick)} 帧（{'抠图后，统一裁掉四周空白，透明处是棋盘格' if kind == 'processed' else '原始帧'}）")
    if notes:
        head += "。注意：" + "；".join(notes)
    return views.contact_sheet(imgs, tile=192), head + "。编号对照：" + "，".join(legend)


@server.tool(name=_scope("view_frames"), title="看帧序列", annotations=READ)
@_wrap_errors
async def view_frames(
        sprite: SpriteRef, action: ActionRef,
        kind: Annotated[Literal["processed", "raw"], Field(
            description="processed = 抠图后（默认，看边缘是否干净）；raw = 原始抽帧")] = "processed",
        max_frames: Annotated[int, Field(ge=1, le=48, description="最多看几帧，均匀抽取")] = 24,
) -> list:
    """把一个动作的帧序列（均匀抽取）拼成编号总览图：看动作是否连贯完整、首尾能否衔接循环、
    抠图有没有残边或缺块（透明处是棋盘格）。"""
    jpeg, text = await _run(_frames_sheet, sprite, action, kind, max_frames)
    return [Image(data=jpeg, format="jpeg"), text]


def _video_sheet(sprite_ref: str, action_ref: str, take_id: Optional[str], samples: int):
    sp = queries.find_sprite(sprite_ref)
    a = queries.find_actions(sp, [action_ref])[0]
    vdir = sprite_store.action_dir(sp["id"], a["id"]) / "video"
    try:
        data = json.loads((vdir / "takes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    takes = [t for t in data.get("takes") or [] if t.get("status") == "succeeded"]
    if not takes:
        raise queries.ToolInputError(f"动作「{a.get('name')}」还没有生成成功的视频")
    tid = take_id or data.get("current") or takes[-1]["id"]
    take = next((t for t in takes if t["id"] == tid), None)
    if take is None:
        raise queries.ToolInputError(f"没有视频版本 {tid}，可选：" + "、".join(t["id"] for t in takes))
    path = vdir / take.get("file", "")
    if not path.is_file():
        raise queries.ToolInputError(f"视频版本 {tid} 的文件不在了")
    imgs, meta = views.video_samples(path, max(2, min(samples, 16)))
    info = {"版本": tid, "当前使用": tid == data.get("current"), "模型": take.get("model"),
            "提示词": f"{take.get('prompt_name') or '?'}"
                    + (f" v{take['prompt_version']}" if take.get("prompt_version") else ""),
            "参考视频模板": queries._template_label(
                queries.template_store.get(take.get("template_id") or "")),
            "生成时间": queries._ts(take.get("created_at")), **meta,
            "全部版本": [t["id"] + ("（当前）" if t["id"] == data.get("current") else "")
                     for t in takes]}
    head = (f"「{sp.get('name')} / {a.get('name')}」视频均匀抽 {len(imgs)} 帧，"
            f"编号 1..{len(imgs)} 按时间先后。\n" + json.dumps(info, ensure_ascii=False))
    return views.contact_sheet(imgs, tile=224), head


@server.tool(name=_scope("view_video"), title="看视频", annotations=READ)
@_wrap_errors
async def view_video(
        sprite: SpriteRef, action: ActionRef,
        take_id: Annotated[Optional[str], Field(description="视频版本 id；不给就看当前使用的那个")] = None,
        samples: Annotated[int, Field(ge=2, le=16, description="均匀抽几帧")] = 8,
) -> list:
    """从动作生成的视频里均匀抽帧拼图，看动作是否符合参考视频、角色有没有走样、背景是否干净，
    并给出这个版本的模型、提示词版本、时长、分辨率等信息。"""
    jpeg, text = await _run(_video_sheet, sprite, action, take_id, samples)
    return [Image(data=jpeg, format="jpeg"), text]


# ---------------------------------------------------------------- 任务
@server.tool(name=_scope("list_jobs"), title="任务列表", annotations=READ)
@_wrap_errors
async def list_jobs(
        only_active: Annotated[bool, Field(description="只看排队中和进行中的（默认）")] = True,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
) -> dict:
    """平台上的后台任务（生成、抽帧、抠图、导出、流水线……）及进度。"""
    return await _run(queries.list_jobs, only_active, limit)


@server.tool(name=_scope("wait_job"), title="等任务", annotations=READ)
@_wrap_errors
async def wait_job(
        job_id: Annotated[str, Field(description="任务 id")],
        max_wait_seconds: Annotated[int, Field(ge=0, le=60, description="最多等几秒，0 = 只查一次")] = 30,
) -> dict:
    """等一个任务结束（或等到上限就返回当前进度）。没结束就再调一次，不要重复提交任务。"""
    from app.services.job_manager import job_manager
    deadline = time.monotonic() + max_wait_seconds
    while True:
        job = job_manager.get(job_id)
        if job is None:
            return {"id": job_id, "status": "unknown", "message": queries.JOB_GONE}
        d = job.to_dict()
        if d["status"] in queries.TERMINAL or time.monotonic() >= deadline:
            v = queries.job_view(d, with_result=True)
            if d["status"] not in queries.TERMINAL:
                v["hint"] = "还没结束，可以再调一次 wait_job 继续等"
            return v
        await anyio.sleep(1)


# ---------------------------------------------------------------- 接入 FastAPI
class McpEndpoint:
    """挂在 /api/mcp 的 ASGI 端点。会话管理器随服务生命周期创建——
    SDK 的管理器只能 run 一次，测试里每个 TestClient 都会重走一遍 lifespan。"""

    manager = None

    async def __call__(self, scope, receive, send):
        if self.manager is None:
            await JSONResponse({"detail": "MCP 服务还没就绪"}, status_code=503)(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


endpoint = McpEndpoint()


@contextlib.asynccontextmanager
async def running():
    from app.security import MCP_PATH
    # DNS 重绑定防护关掉：它只认 Host 白名单，局域网 IP 访问会被拒；这里只认 Bearer、
    # 不认 Cookie，浏览器被重绑定过来也带不上凭证
    server.streamable_http_app(
        streamable_http_path=MCP_PATH, json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    async with server.session_manager.run():
        endpoint.manager = server.session_manager
        try:
            yield
        finally:
            endpoint.manager = None
