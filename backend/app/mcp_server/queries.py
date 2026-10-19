"""MCP 工具背后的查询逻辑。不依赖 MCP SDK，单独可测。

全部只读，不改任何数据。数据都来自现有的存储层与流水线检查（就绪检查用的就是
界面上「计划预览」那一套），界面上看到什么，AI 就看到什么。
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from app.services.sprite_store import SpriteStoreError, sprite_store
from app.services.template_store import template_store

# 流水线步骤的中文名（与界面一致）
STEP_LABEL = {"firstframe": "首帧", "generate": "视频", "extract": "抽帧",
              "matting": "抠图", "export": "导出"}
# 提示词命中的层级 → 说给人听的来源
PROMPT_SOURCE = {"action": "动作单独设定", "template": "模板绑定", "key": "按动作 key 绑定",
                 "group": "分组绑定", "global": "全局默认", "builtin": "内置兜底"}


class ToolInputError(Exception):
    """调用方给的参数不对（找不到角色/动作等）。消息直接给 AI 看，要说清怎么改。"""


def _ts(t) -> Optional[str]:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else None


# ---------------------------------------------------------------- 定位
def find_sprite(ref: str) -> dict:
    """按 id 或名字找角色。重名时要求用 id。"""
    ref = (ref or "").strip()
    if not ref:
        raise ToolInputError("没给角色——传角色的 id 或名字（list_sprites 可查）")
    try:
        return sprite_store.get_sprite(ref)
    except SpriteStoreError:
        pass
    hits = [s for s in sprite_store.list_sprites() if s.get("name") == ref]
    if len(hits) == 1:
        return sprite_store.get_sprite(hits[0]["id"])
    if hits:
        raise ToolInputError(f"有 {len(hits)} 个角色都叫「{ref}」，请改用 id："
                             + "、".join(h["id"] for h in hits))
    raise ToolInputError(f"找不到角色「{ref}」——用 list_sprites 查 id 或名字")


def find_actions(sprite: dict, refs: Optional[List[str]] = None) -> List[dict]:
    """按 id 或名字挑动作；不给就是全部（按角色里的顺序）。"""
    actions = sprite_store.list_actions(sprite["id"])
    if not refs:
        return actions
    by_id = {a["id"]: a for a in actions}
    by_name = defaultdict(list)
    for a in actions:
        by_name[a.get("name")].append(a)
    out, missing = [], []
    for r in refs:
        r = (r or "").strip()
        if r in by_id:
            out.append(by_id[r])
        elif len(by_name.get(r, [])) == 1:
            out.append(by_name[r][0])
        else:
            missing.append(r)
    if missing:
        names = "、".join(a.get("name", a["id"]) for a in actions[:40])
        raise ToolInputError(f"角色「{sprite.get('name')}」里没有动作：{'、'.join(missing)}。"
                             f"现有动作：{names}")
    return out


def _template_label(tpl: Optional[dict]) -> Optional[str]:
    if not tpl:
        return None
    if tpl.get("variant"):
        return f"{tpl.get('key')}（{tpl['variant']}）"
    return tpl.get("key")


# ---------------------------------------------------------------- 角色与进度
def stage_of(action: dict) -> str:
    """动作卡在流水线哪一步（给人看的一句话）。"""
    s = action.get("summary") or {}
    if action.get("status") == "final":
        return "已定稿"
    if not s.get("has_first_frame"):
        return "缺首帧"
    if s.get("generating_count"):
        return "视频生成中"
    if not s.get("has_video"):
        return "缺视频"
    if not s.get("frame_count"):
        return "待抽帧"
    if not s.get("processed_count"):
        return "待抠图"
    if s["processed_count"] < s["frame_count"]:
        return "抠图未完成"
    if not s.get("export_count"):
        return "待导出"
    return "已导出"


def list_sprites(keyword: str = "") -> dict:
    kw = (keyword or "").strip().lower()
    out = []
    for sp in sprite_store.list_sprites():
        if kw and kw not in (sp.get("name") or "").lower() \
                and not any(kw in (t or "").lower() for t in sp.get("tags") or []):
            continue
        stages = Counter(stage_of(a) for a in sprite_store.list_actions(sp["id"]))
        out.append({"id": sp["id"], "name": sp.get("name"), "tags": sp.get("tags") or [],
                    "actions": sum(stages.values()), "stages": dict(stages)})
    return {"sprites": out, "count": len(out)}


def _portraits(sprite_id: str) -> Dict[str, Optional[dict]]:
    refs = sprite_store.list_refs(sprite_id)
    pick = {}
    for role in ("front", "back"):
        pick[role] = next((r for r in refs if (r.get("role") or "") == role), None)
    return pick


def sprite_overview(sprite_ref: str) -> dict:
    from app.core.pipeline import size_ops_of
    sp = find_sprite(sprite_ref)
    sid = sp["id"]
    preset = sp.get("preset") or {}
    m = preset.get("matting") or {}
    out_fmt = (preset.get("output") or {}).get("format") or "frames（序列帧 PNG，默认）"
    por = _portraits(sid)
    actions, stages = [], Counter()
    for a in sprite_store.list_actions(sid):
        s = a.get("summary") or {}
        st = stage_of(a)
        stages[st] += 1
        tpl = template_store.get(a.get("template_id") or "")
        rec = {"id": a["id"], "name": a.get("name"), "stage": st,
               "template": _template_label(tpl),
               "videos": s.get("generated_count", 0),
               "frames": s.get("frame_count", 0), "processed": s.get("processed_count", 0),
               "exports": s.get("export_count", 0)}
        pipe = a.get("pipeline") or {}
        if pipe.get("status") and pipe["status"] != "done":
            rec["pipeline"] = {k: pipe.get(k) for k in ("status", "paused_after", "error")
                               if pipe.get(k)}
        for k, label in (("export_alias", "导出名"), ("spine_anim", "Spine 动画名")):
            if a.get(k):
                rec[label] = a[k]
        if (a.get("gen_prefs") or {}).get("video", {}).get("prompt_text"):
            rec["note"] = "视频提示词是这个动作单独记忆的，偏离了模板"
        actions.append(rec)
    return {
        "sprite": {"id": sid, "name": sp.get("name"), "tags": sp.get("tags") or []},
        "template_binding": sp.get('template_binding') or {},
        "portraits": {"正面立绘": bool(por["front"]), "背面立绘": bool(por["back"])},
        "preset": {"抠图模型": m.get("model") or "（默认）",
                   "抠图参数": {k: m.get(k) for k in ("alpha_threshold", "erode", "feather")
                            if k in m},
                   "导出时处理": size_ops_of(sid) or ["无"],
                   "导出格式": out_fmt},
        "stages": dict(stages),
        "actions": actions,
    }


# ---------------------------------------------------------------- 模板库（定型资产）
def template_library(kind: str = "all") -> dict:
    from app.services.ffset_store import ffset_store
    from app.services.prompt_store import SCOPE_LABEL, PromptStore, prompt_store
    from app.services.spine_template_store import spine_template_store
    out = {}
    if kind in ("all", "reference_videos"):
        used = Counter()
        for sp in sprite_store.list_sprites():
            for a in sprite_store.list_actions(sp["id"]):
                if a.get("template_id"):
                    used[a["template_id"]] += 1
        rows = []
        for t in template_store.list():
            r = t.get("extract_rule") or {}
            rows.append({"id": t["id"], "key": t.get("key"), "variant": t.get("variant") or None,
                         "group": t.get("group") or None,
                         "推荐时长": t.get("duration_hint"),
                         "抽帧规则": (f"{r['start']}–{r['end']}s @{r['fps']}fps" if r else "未设"),
                         "Spine 动画名": t.get("spine_anim") or None,
                         "绑定动作数": used.get(t["id"], 0)})
        out["reference_videos"] = rows
    if kind in ("all", "prompts"):
        rows = []
        for p in prompt_store.list():
            text = PromptStore.current_text(p)
            rows.append({"id": p["id"], "scope": SCOPE_LABEL.get(p.get("scope"), p.get("scope")),
                         "name": p.get("name"), "当前版本": p.get("current"),
                         "版本数": len(p.get("versions") or []),
                         "绑定": [b["level"] if b["level"] == "global"
                                else f"{b['level']}:{b['value']}" for b in p.get("bindings") or []],
                         "内容": text[:80] + ("…" if len(text) > 80 else "")})
        out["prompts"] = rows
    if kind in ("all", "first_frame_sets"):
        out["first_frame_sets"] = [
            {"id": s["id"], "name": s.get("name"), "group": s.get("group") or None,
             "动作数": len(s.get("frames") or []),
             "keys": [f["key"] for f in (s.get("frames") or [])][:40]}
            for s in ffset_store.list()]
    if kind in ("all", "spine_templates"):
        out["spine_templates"] = [
            {"id": t["id"], "name": t.get("name"), "spine": t.get("spine_version"),
             "动画数": len(t.get("animations") or []),
             "已关联动作数": len(t.get("anim_map") or {})}
            for t in spine_template_store.list()]
    if not out:
        raise ToolInputError("kind 只能是 all / reference_videos / prompts / "
                             "first_frame_sets / spine_templates")
    return out


# ---------------------------------------------------------------- 就绪检查
def _step_text(info: dict) -> str:
    st = info.get("status")
    if st == "skip":
        return "跳过：" + (info.get("reason") or "")
    if st == "blocked":
        return "阻塞：" + (info.get("reason") or "")
    detail = (info.get("note") or info.get("set_name") or info.get("template")
              or info.get("rule") or info.get("model") or info.get("format") or "")
    return "执行" + (f"（{detail}）" if detail else "")


def readiness(sprite_ref: str, action_refs: Optional[List[str]] = None,
              steps: Optional[List[str]] = None) -> dict:
    """量产前的体检：每个动作每一步 执行/跳过/阻塞，设定从哪来，要花多少次生成。"""
    from app.config import get_settings
    from app.core.pipeline import PREV_STEP, STEPS, plan_action, size_ops_of
    from app.services.prompt_store import resolve_for_action
    sp = find_sprite(sprite_ref)
    sid = sp["id"]
    steps = [s for s in STEPS if s in (steps or STEPS)]
    if not steps:
        raise ToolInputError(f"steps 只能取 {', '.join(STEPS)}")
    opts = {"steps": steps, "force": [], "set_id": None}
    s = get_settings()

    rows, blockers, warnings = [], defaultdict(list), defaultdict(list)
    blocker_tpls = defaultdict(set)
    images = videos = seconds = cascaded = 0
    for a in find_actions(sp, action_refs):
        name = a.get("name") or a["id"]
        plan = plan_action(sid, a, opts)
        tpl = template_store.get(a.get("template_id") or "")
        mem = (a.get("gen_prefs") or {}).get("video") or {}
        duration = mem.get("duration") or (tpl or {}).get("duration_hint") or 4
        dur_src = ("动作单独设定" if mem.get("duration")
                   else "模板推荐" if (tpl or {}).get("duration_hint") else "默认")
        settings = {"参考视频模板": _template_label(tpl) or "未绑定"}
        for scope, label in (("first_frame", "首帧提示词"), ("video_ref", "视频提示词")):
            try:
                r = resolve_for_action(scope, a)
                src = r.get("source")
                ver = f" v{r['version']}" if r.get("version") else ""
                settings[label] = f"{r.get('name')}{ver}（{PROMPT_SOURCE.get(src, src)}）"
                if src == "builtin":
                    warnings["提示词用的是内置兜底，没有定型过"].append(f"{name}·{label}")
                elif src == "action":
                    warnings["提示词是动作单独设定的，偏离了模板"].append(f"{name}·{label}")
            except Exception as e:                       # 解析失败不该拖垮整张表
                settings[label] = f"解析失败：{e}"
        settings["视频时长"] = f"{duration}s（{dur_src}）"

        step_view = {}
        for step in steps:
            info = (plan.get("steps") or {}).get(step) or {}
            step_view[STEP_LABEL[step]] = _step_text(info)
            if info.get("status") == "blocked":
                reason = info.get("reason") or ""
                prev = PREV_STEP.get(step)
                if "将由上一步" in reason and prev in steps:
                    # 连带阻塞：前一步过不去，这一步自然没有输入。只算根因，别让 AI 看花眼
                    step_view[STEP_LABEL[step]] = f"阻塞：等上一步（{STEP_LABEL[prev]}）先通过"
                    cascaded += 1
                    continue
                key = f"{STEP_LABEL[step]}：{reason}"
                blockers[key].append(name)
                blocker_tpls[key].add(_template_label(tpl) or "未绑定模板")
            elif info.get("status") == "run":
                if step == "firstframe":
                    images += 1
                elif step == "generate":
                    videos += 1
                    seconds += float(duration)
        rows.append({"id": a["id"], "name": name, "steps": step_view, "settings": settings})

    blocked_n = len({r["name"] for r in rows
                     if any(v.startswith("阻塞") for v in r["steps"].values())})
    verdict = (f"就绪：{len(rows)} 个动作都能直接跑" if not blockers
               else f"有 {blocked_n}/{len(rows)} 个动作被阻塞，先补齐再量产")
    return {
        "sprite": {"id": sid, "name": sp.get("name")},
        "verdict": verdict,
        # 根因。同一原因落在很多动作、且涉及的模板就那几个 → 多半是模板没定型好，该找人改模板
        "blockers": [{"原因": k, "动作": v, "涉及模板数": len(blocker_tpls[k]),
                      "涉及模板": sorted(blocker_tpls[k])[:10]}
                     for k, v in blockers.items()],
        "连带阻塞": (f"另有 {cascaded} 处是前一步过不去连带的，补齐根因后自动解除"
                 if cascaded else None),
        "warnings": [{"问题": k, "涉及": v} for k, v in warnings.items()],
        "paid_estimate": {"首帧生图": images, "视频生成": videos,
                          "视频总时长": f"{seconds:g}s",
                          "视频模型": s.ark_model, "分辨率": "480p（流水线默认）",
                          "说明": "只统计次数，金额以火山方舟计费为准；已完成的步骤会跳过，不重复花钱"},
        "export_ops": size_ops_of(sid) or ["无"],
        "actions": rows,
    }


# ---------------------------------------------------------------- 任务
def job_view(job: dict, with_result: bool = False) -> dict:
    v = {k: job.get(k) for k in ("id", "type", "status", "progress", "message", "error",
                               "resource", "targets", "memory_mb", "interrupted", "metadata")}
    v["created"] = _ts(job.get("created_at"))
    if v.get('metadata'):
        v['metadata'] = {k: value for k, value in v['metadata'].items() if k != 'plan'}
    v["finished"] = _ts(job.get("finished_at"))
    if with_result and job.get("result") is not None:
        txt = json.dumps(job["result"], ensure_ascii=False, default=str)
        v["result"] = job["result"] if len(txt) <= 2000 else txt[:2000] + "…"
    return {k: x for k, x in v.items() if x not in (None, "")}


ACTIVE = ("queued", "running")
TERMINAL = ("done", "error", "cancelled")
JOB_GONE = ("未找到任务记录（可能早于任务持久化版本）。不要直接重新提交"
            "（生成类任务会重复花钱）：先用 get_sprite_overview 看对应动作的实际进度。")


def list_jobs(only_active: bool = True, limit: int = 30) -> dict:
    from app.services.job_manager import job_manager
    jobs = job_manager.list(limit=200)
    if only_active:
        jobs = [j for j in jobs if j.get("status") in ACTIVE]
    return {"jobs": [job_view(j) for j in jobs[:max(1, min(limit, 100))]],
            "active": sum(1 for j in jobs if j.get("status") in ACTIVE)}


def platform_status() -> dict:
    from app.api.capabilities import capabilities
    from app.config import get_settings
    from app.core.background_remover import BackgroundRemover
    from app.core.oss_uploader import oss_configured
    from app.services.job_manager import job_manager
    s = get_settings()
    cap = capabilities()
    plat = cap.get("platform") or {}
    models = [m["name"] for m in cap.get("background_models") or [] if m.get("installed")]
    jobs = job_manager.list(limit=200)
    from app.services.resources import resources
    return {"gpu": plat.get("gpu_available"), "抠图模型": models,
            "默认抠图模型": BackgroundRemover.default_model(),
            "视频生成": {"已配置": s.generate_enabled, "模型": s.ark_model,
                       "同时进行上限": s.generate_max_concurrent},
            "OSS": oss_configured(), "本地调度": resources.status(),
            "进行中的任务": sum(1 for j in jobs if j.get("status") in ACTIVE)}
