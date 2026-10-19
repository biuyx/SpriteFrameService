"""MCP 二期免费加工。只解析模板/工艺预设，不接受临时生成参数。"""
from __future__ import annotations
import copy

from app.api.deps import get_session
from app.core import pipeline
from app.mcp_server import queries
from app.services.job_manager import JobSubmissionError, job_manager
from app.services.resources import estimate_export_mb, estimate_extract_mb, resources
from app.services.sprite_store import sprite_store

FREE_STEPS = ('extract', 'matting', 'export')


def _resolve(sprite, actions, steps, force=False):
    if not steps or set(steps) - set(FREE_STEPS):
        raise queries.ToolInputError('只能选择免费工序 extract、matting、export；不支持生图或视频生成')
    sp = queries.find_sprite(sprite)
    acts = queries.find_actions(sp, actions)
    acts = list({a['id']: a for a in acts}.values())
    if not acts or len(acts) > 50:
        raise queries.ToolInputError('每批请选择 1–50 个动作')
    ordered = [s for s in FREE_STEPS if s in steps]
    request = {'sprite': sp['id'], 'actions': sorted(a['id'] for a in acts),
               'steps': ordered, 'force': force}
    return sp, acts, request


def _action_plan(sp, action, request):
    from app.models.export_config import ExportConfig
    session = get_session(action['id'])
    _, rule = pipeline._rule_for(session, action)
    out = dict((sp.get('preset') or {}).get('output') or pipeline.DEFAULT_OUTPUT_PRESET)
    for key in ('name_pattern', 'output_path', 'output_name', 'frame_indices', 'scale', 'outline'):
        out.pop(key, None)
    from app.api.export_api import _resolve_name
    name = _resolve_name(action['id'], action.get('export_alias') or action.get('name') or action['id'])
    config = ExportConfig(**out, output_name=name,
                          scale=pipeline._preset(sp['id'], 'scale', pipeline.DEFAULT_SCALE_PRESET),
                          outline=pipeline._preset(sp['id'], 'outline', pipeline.DEFAULT_OUTLINE_PRESET))
    matting = dict((sp.get('preset') or {}).get('matting') or {})
    from app.core.background_remover import BackgroundRemover
    matting = {'model': matting.get('model') or BackgroundRemover.default_model(),
               'alpha_threshold': matting.get('alpha_threshold', 128),
               'erode': matting.get('erode', 1), 'feather': matting.get('feather', 0)}
    opts = {'steps': request['steps'], 'force': request['steps'] if request['force'] else []}
    return {'id': action['id'], 'name': action['name'],
            'plan': pipeline.plan_action(sp['id'], action, opts),
            'settings': {'template_id': action.get('template_id'), 'extract_rule': copy.deepcopy(rule),
                         'matting': matting, 'export': config.model_dump(mode='json')}}


def plan_processing(sprite, actions, steps, force=False):
    sp, acts, request = _resolve(sprite, actions, steps, force)
    return {'request': request, 'paid_calls': 0,
            'actions': [_action_plan(sp, a, request) for a in acts],
            'resources': resources.status()}


def submit_processing(sprite, actions, steps, request_id, force=False, caller='local'):
    request_id = request_id.strip()
    if not request_id or len(request_id) > 128:
        raise queries.ToolInputError('请求编号必须是 1–128 个字符；网络重试沿用原编号，明确重做用新编号')
    sp, acts, request = _resolve(sprite, actions, steps, force)
    key = f'mcp-free:{caller}:{request_id}'
    existing = job_manager.by_request_key(key)
    if existing:
        if existing.metadata.get('request') != request:
            raise JobSubmissionError('请求编号已用于其他参数，请使用新的请求编号')
        return batch_view(existing.id)
    plan = {'request': request, 'paid_calls': 0, 'actions': [_action_plan(sp, a, request) for a in acts]}
    blocked = [f"{a['name']} / {step}: {info.get('reason')}" for a in plan['actions']
               for step, info in a['plan']['steps'].items() if info['status'] == 'blocked']
    if blocked:
        raise queries.ToolInputError('批次未提交，请先解决：' + '；'.join(blocked[:10]))
    job = job_manager.submit('processing_batch', lambda ctx: _run_batch(plan, ctx), pool='io',
                             params=request, request_key=key,
                             metadata={'request': request, 'plan': plan, 'paid_calls': 0},
                             resource='orchestrator')
    return batch_view(job.id)


def batch_view(batch_id):
    job = job_manager.get(batch_id)
    if job is None or job.type != 'processing_batch':
        raise queries.ToolInputError('找不到加工批次，请使用 submit_processing_batch 返回的 batch_id')
    return {'batch_id': job.id, 'status': job.status.value, 'progress': job.progress,
            'message': job.message, 'error': job.error, 'interrupted': job.interrupted,
            'request': job.metadata.get('request'), 'plan': job.metadata.get('plan'),
            'result': job.result, 'paid_calls': 0}


def cancel_processing(job_id):
    job = job_manager.get(job_id)
    if job is None:
        raise queries.ToolInputError('找不到任务')
    if job.type not in ('processing_batch', 'extract', 'background', 'export', 'spine_export'):
        raise queries.ToolInputError('operate 权限只能取消免费加工任务，不能取消付费生成或混合流水线')
    return {'job_id': job_id, 'cancel_requested': job_manager.cancel(job_id)}


def _run_batch(plan, ctx):
    from app.api.background import run_bg_remove
    from app.api.frames import run_extract
    from app.api.export_api import run_export
    from app.config import get_settings
    from app.models.export_config import ExportConfig
    request = plan['request']
    results = []
    total = len(plan['actions']) * len(request['steps'])
    completed = 0
    for row in plan['actions']:
        entry = {'action_id': row['id'], 'name': row['name'], 'steps': {}}
        results.append(entry)
        for step in request['steps']:
            if ctx.cancelled():
                return {'actions': results}
            session = get_session(row['id'])
            # Finished steps need neither their original sources nor a resource
            # reservation. Recheck live state under the session lock, not the
            # preview snapshot; actual work still checks again after admission.
            with session.lock:
                action = sprite_store.get_action(request['sprite'], row['id'])
                skip = not request['force'] and pipeline._is_done(step, request['sprite'], action, session)
            if skip:
                entry['steps'][step] = {'status': 'skipped', 'reason': '已完成'}
                completed += 1
                ctx.set_result(copy.deepcopy({'actions': results}))
                ctx.report(completed / total * 100, f"已完成 {completed}/{total} 道工序")
                continue
            settings = row['settings']
            resource = 'export' if step == 'export' else 'matting' if step == 'matting' else 'heavy'
            cfg = ExportConfig(**settings['export'])
            indices = list(range(session.frame_manager.frame_count))
            if step == 'extract':
                rule = settings['extract_rule']
                if not rule or session.video_info is None:
                    raise pipeline.StepBlocked('视频或模板抽帧规则已不可用，请重新预览')
                amount = estimate_extract_mb(session, float(rule['start']),
                    min(float(rule['end']), session.video_info.duration), float(rule['fps']))
            else:
                amount = (estimate_export_mb(session, indices, cfg) if step == 'export'
                          else get_settings().matting_memory_mb)

            class Sub:
                def cancelled(self): return ctx.cancelled()
                def register_cancel(self, fn): ctx.register_cancel(fn)
                def report(self, percent, message=''):
                    ctx.report((completed + percent / 100) / total * 100,
                               f"{row['name']} / {pipeline.STEP_LABEL[step]}：{message}")

            sub = Sub()
            with resources.hold(sub, resource, [session.id], amount), session.lock:
                action = sprite_store.get_action(request['sprite'], row['id'])
                if not request['force'] and pipeline._is_done(step, request['sprite'], action, session):
                    entry['steps'][step] = {'status': 'skipped', 'reason': '已完成'}
                elif step == 'extract':
                    from app.services.template_bindings import validate_reference
                    validate_reference(request['sprite'], settings.get('template_id'))
                    pipeline.check_extract(request['sprite'], action, {}, session)
                    rule = settings['extract_rule']
                    if not rule or session.video_info is None:
                        raise pipeline.StepBlocked('视频或模板抽帧规则已不可用，请重新预览')
                    result = run_extract(session, float(rule['start']),
                                         min(float(rule['end']), session.video_info.duration),
                                         float(rule['fps']), rule.get('keep'), rule.get('total'), sub)
                    entry['steps'][step] = {'status': 'done', 'result': result}
                elif step == 'matting':
                    indices = [f.index for f in session.frame_manager.frames
                               if request['force'] or not f.has_processed]
                    if not indices:
                        raise pipeline.StepBlocked('没有可抠图的帧，请重新预览')
                    result = run_bg_remove(session, indices, 'ai', settings['matting'], sub)
                    entry['steps'][step] = {'status': 'done', 'result': result}
                else:
                    pipeline.check_export(request['sprite'], action, {}, session)
                    indices = [f.index for f in session.frame_manager.frames if f.has_processed]
                    result = run_export(session, cfg, indices, cfg.output_name, sub)
                    entry['steps'][step] = {'status': 'done', 'result': result}
            completed += 1
            ctx.set_result(copy.deepcopy({'actions': results}))
            ctx.report(completed / total * 100, f"已完成 {completed}/{total} 道工序")
    return {'actions': results}
