"""Local administrator CLI. No approval, quota or qualification writes over MCP."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser(description='MCP 付费计划审批、累计额度和工艺定型')
    sub = parser.add_subparsers(dest='command', required=True)
    quota = sub.add_parser('quota')
    quota.add_argument('caller', help='AI 密钥记录的 id，不是名字或明文密钥')
    quota.add_argument('--calls', type=int, required=True)
    quota.add_argument('--seconds', type=int, required=True)
    approve = sub.add_parser('approve')
    approve.add_argument('caller')
    approve.add_argument('plan_file', type=Path)
    approve.add_argument('--ttl', type=int, default=3600)
    qualify = sub.add_parser('qualify')
    qualify.add_argument('batch', help='成功试产的 ledger_batch')
    qualify.add_argument('--note', required=True)
    revoke = sub.add_parser('revoke')
    revoke.add_argument('approval')
    draft = sub.add_parser('draft')
    draft.add_argument('recipe', help='撤回工艺定型；阻止相关量产的后续付费调用')
    close = sub.add_parser('close')
    close.add_argument('batch', help='阻止该批次未来调用；释放未尝试额度，已尝试的不退')
    report = sub.add_parser('report')
    report.add_argument('caller')
    args = parser.parse_args()
    from app.services.generation_ledger import generation_ledger as ledger
    from app.services.job_manager import JobSubmissionError
    try:
        if args.command == 'quota':
            ledger.set_quota(args.caller, args.calls, args.seconds)
            result = ledger.report(args.caller)
        elif args.command == 'approve':
            from app.mcp_server.generation import validate_plan
            plan = json.loads(args.plan_file.read_text(encoding='utf-8-sig'))
            validate_plan(plan)
            result = {'approval_id': ledger.approve(args.caller, plan, args.ttl),
                      'fingerprint': plan['fingerprint'], 'estimate': plan['estimate']}
        elif args.command == 'qualify':
            ledger.qualify(args.batch, args.note)
            result = {'qualified_trial': args.batch}
        elif args.command == 'revoke':
            ledger.revoke(args.approval)
            result = {'revoked': args.approval}
        elif args.command == 'draft':
            ledger.mark_draft(args.recipe)
            result = {'draft_recipe': args.recipe}
        elif args.command == 'close':
            ledger.close(args.batch)
            result = {'closed': args.batch}
        else:
            result = ledger.report(args.caller)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, JobSubmissionError) as exc:
        print(f'失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    os.environ.setdefault('SPRITE_WARMUP_MODEL', 'false')
    sys.exit(main())
