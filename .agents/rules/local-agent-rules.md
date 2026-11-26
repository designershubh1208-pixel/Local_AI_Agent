---
trigger: always_on
---

# Local AI Agent Workspace Rules
---
trigger: always_on
description: "Hardware limits, credential safety, and human-approval rules for the local Windows AI agent."
---
- Target hardware: Windows laptop with RTX 4050 (6GB VRAM), 16GB RAM, and Ryzen 7 7480HS.
- Local models via Ollama (`http://localhost:11434`): use `qwen3:8b` for email/file/task routing and `qwen2.5-coder:14b` for complex code generation/debugging.
- Keep `num_ctx` at `2048` by default to prevent memory pressure on 16GB RAM.
- Never store raw passwords or API tokens in code or `.env` files; always use Windows Credential Manager (`keyring`).
- Restrict file operations to `E:\local_ai_agent\workspace` unless explicitly approved, and always require human confirmation before sending real emails.