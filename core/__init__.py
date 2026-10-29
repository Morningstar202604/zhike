"""知客 core 包。

统一在包导入时加载 .env，保证 `python -m uvicorn core.main:app`、`python gateway/bot.py`、
测试与脚本看到同一份配置。必须在任何模块读取 os.environ 之前执行，因此放在包初始化。
"""
from core.env import load_env as _load_env

_load_env()
