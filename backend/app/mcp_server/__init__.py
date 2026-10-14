"""给 AI 客户端用的 MCP 服务（Claude Code 等）。

server.py 依赖 mcp SDK；queries.py / views.py / audit.py 不依赖，单独可测。
没装 mcp 包时平台照常启动，只是没有 /api/mcp。
"""
