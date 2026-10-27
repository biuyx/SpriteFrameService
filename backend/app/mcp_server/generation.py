"""Trial -> reviewed recipe -> production, with admin-approved immutable plans."""
from __future__ import annotations

import hashlib
import json
import threading
import time

from app.api.deps import get_session
from app.config import get_settings
from app.core import pipeline
from app.mcp_server import queries
from app.services.generation_ledger import generation_ledger
from app.services.job_manager import JobSubmissionError, job_manager
from app.services.sprite_store import sprite_store
from app.services.template_store import template_store

_submission_lock = threading.Lock()


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def plan_generation(sprite, actions, steps, mode='trial', force=False,
                    image_model=None, video_model=None):
    from app.api.generate import _default_generate_payload
    from app.core.first_frame_generator import resolve_refs
    from app.services.prompt_store import resolve_for_action
    if mode not in ('trial', 'production') or not steps or set(steps) - {'firstframe', 'generate'}:
        raise queries.ToolInputError('mode 请选择 trial/production；付费工序只支持 firstframe、generate')
    settings = get_settings()
    # 指定模型必须是下拉里真有的，否则等于让人审批一个跑不起来的计划
    if image_model and image_model not in {m['id'] for m in settings.image_models_list}:
        raise queries.ToolInputError(
            'image_model 不在可选列表：' +
            '、'.join(f"{m['id']}（{m['label']}）" for m in settings.image_models_list))
    if video_model and video_model not in {m['id'] for m in settings.video_models_list}:
        raise queries.ToolInputError(
            'video_model 不在可选列表：' +
            '、'.join(f"{m['id']}（{m['label']}）" for m in settings.video_models_list))
    image_model = image_model or None
    video_model = video_model or None
    sp = queries.find_sprite(sprite)
    acts = sorted({a['id']: a for a in queries.find_actions(sp, actions)}.values(), key=lambda a: a['id'])
    if not 1 <= len(acts) <= (1 if mode == 'trial' else 50):
        raise queries.ToolInputError('试产每批仅 1 个动作；量产每批 1–50 个动作')
    steps = [s for s in ('firstframe', 'generate') if s in steps]
    request = {'sprite': sp['id'], 'actions': [a['id'] for a in acts],
               'steps': steps, 'mode': mode, 'force': force}
    blockers, items = [], []
    if not settings.generate_enabled:
        blockers.append('未配置 Ark API Key')
    for action in acts:
        row = pipeline.plan_action(sp['id'], action, {'steps': steps, 'force': steps if force else []})
        makes_frame = row['steps'].get('firstframe', {}).get('status') == 'run'
        for step in steps:
            info = row['steps'][step]
            if info['status'] == 'blocked':
                blockers.append(f"{action['name']} / {step}: {info.get('reason')}")
                continue
            if info['status'] != 'run':
                continue
            tpl = template_store.get(action.get('template_id') or '')
            item = {'action': action['id'], 'step': step, 'seconds': 0}
            try:
                if step == 'firstframe':
                    ff = pipeline.check_firstframe(sp['id'], action, {})
                    art, ref, key, role = resolve_refs(sp['id'], action, ff['set_id'])
                    prompt = resolve_for_action('first_frame', action)
                    chosen = image_model or settings.ark_image_model
                    item.update(set_id=ff['set_id'], prompt=prompt,
                                model=chosen,
                                assets={'art': digest(art), 'reference': digest(ref)}, key=key, role=role)
                    recipe = {'step': step, 'key': key, 'prompt': prompt,
                              'reference': digest(ref), 'model': chosen}
                else:
                    from app.core.oss_uploader import oss_configured
                    if not oss_configured():
                        raise ValueError('未配置 OSS，参考视频无法供生成接口访问')
                    chosen = video_model or settings.ark_model
                    if chosen not in {m['id'] for m in settings.video_models_list}:
                        raise ValueError('视频模型不在允许列表')
                    if not tpl:
                        raise ValueError('未绑定参考视频模板')
                    payload = _default_generate_payload(get_session(action['id']), action, tpl,
                                                         chosen, '480p', 'adaptive')
                    duration = payload['params']['duration']
                    if type(duration) is not int or not 4 <= duration <= 12:
                        raise ValueError('视频时长须为 4–12 秒的整数')
                    media = template_store.path(tpl).read_bytes()
                    first = ('generated' if makes_frame else digest(
                        (sprite_store.action_dir(sp['id'], action['id']) / 'first_frame.png').read_bytes()))
                    item.update(payload=payload, seconds=duration,
                                assets={'video': digest(media), 'first_frame': first})
                    recipe = {'step': step, 'payload': payload, 'video': digest(media),
                              'extract_rule': tpl.get('extract_rule')}
                recipe['binding_group'] = (sp.get('template_binding') or {}).get('group')
                item['recipe'] = digest(recipe)
                item['recipe_state'] = ('qualified' if generation_ledger.is_qualified(item['recipe']) else 'draft')
                if mode == 'production' and item['recipe_state'] != 'qualified':
                    blockers.append(f"{action['name']} / {step}: 工艺仍为草稿，请先试产并人工定型")
                items.append(item)
            except (OSError, ValueError) as exc:
                blockers.append(f"{action['name']} / {step}: {exc}")
    plan = {'schema': 1, 'request': request, 'mode': mode, 'items': items,
            'template_binding': sp.get('template_binding') or {},
            'blockers': blockers, 'estimate': {'calls': len(items), 'video_seconds': sum(i['seconds'] for i in items)},
            'currency_cost': None}
    plan['fingerprint'] = digest(plan)
    return plan


def validate_plan(plan):
    if plan != plan_generation(**plan['request']):
        raise JobSubmissionError('角色绑定、素材、提示词、模型或模板已变化，请重新规划并审批')


def submit_generation(approval_id, request_id, caller='local'):
    with _submission_lock:
        # A single approval can only fund one batch. Retry uses the same durable job key.
        approval = generation_ledger.approval(approval_id, caller)
        key = f'mcp-paid:{caller}:{request_id}'
        old = job_manager.by_request_key(key)
        if old:
            if old.metadata.get('approval') != approval_id:
                raise JobSubmissionError('请求编号已用于其他审批')
            return generation_batch(old.id, caller)
        validate_plan(approval['plan'])
        batch = generation_ledger.reserve(approval_id, caller, request_id)
        job = job_manager.submit('generation_batch',
            lambda ctx: _execute(approval_id, caller, batch['id'], ctx),
            pool='io', resource='orchestrator', params={'approval': approval_id},
            request_key=key, metadata={'approval': approval_id, 'ledger_batch': batch['id'], 'caller': caller})
        return generation_batch(job.id, caller)


def generation_batch(job_id, caller='local'):
    job = job_manager.get(job_id)
    if not job or job.type != 'generation_batch' or job.metadata.get('caller') != caller:
        raise queries.ToolInputError('找不到属于当前调用方的生成批次')
    return {'job_id': job.id, 'ledger_batch': job.metadata['ledger_batch'],
            'status': job.status.value, 'progress': job.progress, 'message': job.message,
            'result': job.result, 'error': job.error, 'interrupted': job.interrupted}


def cancel_generation(job_id, caller='local'):
    view = generation_batch(job_id, caller)
    cancelled = job_manager.cancel(job_id)
    generation_ledger.close(view['ledger_batch'])
    return {'job_id': job_id, 'cancel_requested': cancelled,
            'note': '停止后续调用并尝试取消远端任务；已尝试的生成仍计入额度，不保证供应商退款'}


def _execute(approval_id, caller, batch, ctx):
    from app.api.generate import generate_gate
    from app.core.first_frame_generator import first_frame_gate, resolve_refs, run_gen_first_frame
    from app.core.video_generator import run_generate
    plan = generation_ledger.approval(approval_id, caller)['plan']
    results = []
    try:
        validate_plan(plan)  # Queue time is allowed to be long; approval may have gone stale.
        for index, item in enumerate(plan['items']):
            if ctx.cancelled():
                break
            session = get_session(item['action'])
            gate = first_frame_gate if item['step'] == 'firstframe' else generate_gate
            with gate.hold(ctx), session.lock:
                approved = generation_ledger.approval(approval_id, caller)
                if approved['revoked'] or approved['expires'] < time.time():
                    raise JobSubmissionError('排队期间审批已撤销或过期')
                # A batch can wait inside the provider gate after its first validation.
                # Re-resolve this step after admission, without considering completion skips.
                fresh = plan_generation(plan['request']['sprite'], [item['action']],
                                        [item['step']], mode='trial', force=True)
                if (fresh['blockers'] or not fresh['items'] or
                        fresh['items'][0]['recipe'] != item['recipe']):
                    raise JobSubmissionError('排队期间工艺或提示词已变化，请重新规划审批')
                action = sprite_store.get_action(plan['request']['sprite'], item['action'])
                if item['step'] == 'firstframe':
                    refs = resolve_refs(plan['request']['sprite'], action, item['set_id'])
                    if {'art': digest(refs[0]), 'reference': digest(refs[1])} != item['assets']:
                        raise JobSubmissionError('排队期间参考图已变化，请重新规划')
                else:
                    tpl = template_store.get(item['payload']['template_id'])
                    media = template_store.path(tpl).read_bytes() if tpl else b''
                    first = (session.storage.root / 'first_frame.png').read_bytes()
                    expected = item['assets']['first_frame']
                    if expected == 'generated':
                        expected = next(r['first_frame_hash'] for r in results
                                        if r['action'] == item['action'] and r['step'] == 'firstframe')
                    if digest(media) != item['assets']['video'] or digest(first) != expected:
                        raise JobSubmissionError('排队期间视频或首帧已变化，请重新规划')

                class Sub:
                    @property
                    def job_id(self): return getattr(ctx, 'job_id', None)
                    def report(self, percent, message=''):
                        ctx.report((index + percent / 100) / len(plan['items']) * 100, message)
                    def cancelled(self): return ctx.cancelled()
                    def register_cancel(self, fn): ctx.register_cancel(fn)
                    def before_paid_call(self):
                        if ctx.cancelled():
                            raise JobSubmissionError('已取消')
                        generation_ledger.dispatch(batch, index, item['seconds'])
                    def record_remote(self, remote_id):
                        generation_ledger.remote(batch, index, remote_id)

                sub = Sub()
                if item['step'] == 'firstframe':
                    result = run_gen_first_frame(plan['request']['sprite'], item['action'], item['set_id'],
                        item['prompt']['text'], sub, resolved_refs=refs, image_model=item['model'])
                    result['first_frame_hash'] = digest((session.storage.root / 'first_frame.png').read_bytes())
                else:
                    result = run_generate(session, dict(item['payload'], _reference_video_bytes=media,
                                                        _first_frame_bytes=first), sub)
                generation_ledger.complete(batch, index, result)
                results.append({'action': item['action'], 'step': item['step'], **result})
                ctx.set_result({'actions': results})
        return {'actions': results}
    finally:
        generation_ledger.close(batch)
