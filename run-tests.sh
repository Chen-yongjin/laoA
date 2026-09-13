#!/usr/bin/env bash
# 在 Linux 上跑本项目的离线测试（数据层/策略/池子/规则/条件单/通知降级）。
# Windows 上用：.venv\Scripts\python -m pytest tests -q
set -e
cd "$(dirname "$0")"
PY="${PY:-/tmp/sqx-venv/bin/python}"
echo "=== 依赖检查 ==="
"$PY" - <<'PYEOF'
import importlib
for mod in ("pandas", "requests"):
    try:
        importlib.import_module(mod); print(f"  {mod}: OK")
    except ImportError:
        print(f"  {mod}: 缺失（pip install -e . 后重试）")
PYEOF
echo "=== 跑测试 ==="
PYTHONPATH=src "$PY" -m pytest tests -q "$@"
