"""AI 专用密钥管理（给 Claude Code 等 MCP 客户端用）。在服务器上运行：

    .venv/bin/python scripts/agent_keys.py add claude-code              # 默认只读
    .venv/bin/python scripts/agent_keys.py add 夜间批量 --scopes read,operate --note "定时任务用"
    .venv/bin/python scripts/agent_keys.py list
    .venv/bin/python scripts/agent_keys.py revoke <id>

密钥只在 add 时显示一次，库里只存摘要；丢了就吊销重发。
权限范围：read 查询与看图 / operate 免费加工 / generate 已审批且受额度约束的付费生成。
发出去的密钥只能访问 /api/mcp，调平台其他接口一律 401。不用重启服务，即发即用。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 专用密钥管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="发一把新密钥")
    a.add_argument("name", help="名字（审计日志里靠它认人），如 claude-code、张三的 Claude")
    a.add_argument("--scopes", default="read", help="逗号分隔：read,operate,generate（默认 read）")
    a.add_argument("--note", default="", help="备注")
    sub.add_parser("list", help="列出全部密钥")
    r = sub.add_parser("revoke", help="吊销一把密钥")
    r.add_argument("id")
    args = ap.parse_args()

    from app.services.agent_keys import AgentKeyError, agent_key_store
    try:
        if args.cmd == "add":
            scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
            rec, key = agent_key_store.create(args.name, scopes, args.note)
            print(f"已创建「{rec['name']}」（id {rec['id']}，权限 {', '.join(rec['scopes'])}）")
            print("\n密钥（只显示这一次，请直接交给使用者）：\n")
            print(f"    {key}\n")
            print("Claude Code 接入：")
            print(f"    claude mcp add --transport http sprite http://<服务器地址>:8000/api/mcp "
                  f"--header \"Authorization: Bearer {key}\"")
        elif args.cmd == "list":
            keys = agent_key_store.list()
            if not keys:
                print("还没有发过密钥")
            for k in keys:
                state = f"已吊销 {_ts(k.get('revoked_at'))}" if k.get("revoked_at") else "有效"
                print(f"{k['id']}  {k['name']:<16} {','.join(k['scopes']):<22} {state:<20} "
                      f"创建 {_ts(k.get('created_at'))}  最近使用 {_ts(k.get('last_used_at'))}"
                      + (f"  备注：{k['note']}" if k.get("note") else ""))
        elif args.cmd == "revoke":
            k = agent_key_store.revoke(args.id)
            print(f"已吊销「{k['name']}」（{k['id']}），立即生效")
    except AgentKeyError as e:
        print(f"失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SPRITE_WARMUP_MODEL", "false")
    sys.exit(main())
