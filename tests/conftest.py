"""
共用 pytest fixtures

在导入 x_timeline / app 之前必须先设置环境变量，
否则模块级 _require_env() 会抛出 RuntimeError。
"""
import os
import pytest

# ── 在任何模块导入前设置环境变量 ──────────────────────────────────────────────
os.environ.setdefault("X_AUTH_TOKEN", "test_auth_token_for_pytest")
os.environ.setdefault("X_CT0",        "test_ct0_for_pytest")
