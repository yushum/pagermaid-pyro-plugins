import asyncio
import importlib.util
import os
import sys
from pathlib import Path


if os.environ.get("PGM_PLUGIN_TESTING") != "1":
    raise RuntimeError("请在独立容器中设置 PGM_PLUGIN_TESTING=1 后运行测试。")

asyncio.set_event_loop(asyncio.new_event_loop())

from pagermaid.services import bot, sqlite


def load_plugin(name):
    module_name = f"review_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).parents[1] / name / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
