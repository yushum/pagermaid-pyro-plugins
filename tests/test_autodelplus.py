import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


class FakeSqlite(dict):
    pass


class MessageIdInvalid(Exception):
    pass


def load_plugin():
    sqlite = FakeSqlite()

    pyrogram = types.ModuleType("pyrogram")
    errors = types.ModuleType("pyrogram.errors")
    errors.FloodWait = type("FloodWait", (Exception,), {})
    errors.MessageIdInvalid = MessageIdInvalid
    pyrogram.errors = errors

    pagermaid = types.ModuleType("pagermaid")
    services = types.ModuleType("pagermaid.services")
    services.bot = object()
    services.sqlite = sqlite
    enums = types.ModuleType("pagermaid.enums")
    enums.Message = object
    listener_module = types.ModuleType("pagermaid.listener")
    listener_module.listener = lambda *args, **kwargs: lambda func: func
    utils = types.ModuleType("pagermaid.utils")
    utils.alias_command = lambda command: command
    utils.logs = types.SimpleNamespace(
        warning=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    modules = {
        "pyrogram": pyrogram,
        "pyrogram.errors": errors,
        "pagermaid": pagermaid,
        "pagermaid.services": services,
        "pagermaid.enums": enums,
        "pagermaid.listener": listener_module,
        "pagermaid.utils": utils,
    }
    old_modules = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        path = Path(__file__).parents[1] / "autodelplus" / "main.py"
        spec = importlib.util.spec_from_file_location("autodelplus_under_test", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module, sqlite
    finally:
        for name, old_module in old_modules.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class AutoDeleteSchedulerTests(unittest.TestCase):
    def test_queue_does_not_silently_drop_jobs_after_5000(self):
        module, _ = load_plugin()
        scheduler = module.AutoDeleteScheduler()

        for message_id in range(1, 5002):
            self.assertTrue(
                scheduler.add_job(module.DeleteJob(100, -1, message_id, "chat"))
            )

        self.assertEqual(len(scheduler.live), 5001)

    def test_init_merges_new_jobs_and_skips_cancelled_snapshot(self):
        module, sqlite = load_plugin()
        scheduler = module.AutoDeleteScheduler()
        loaded = module.DeleteJob(100, -1, 1, "chat")
        added = module.DeleteJob(101, -1, 2, "chat")
        cancelled = module.DeleteJob(102, -1, 3, "chat")
        sqlite[module._job_key(-1, 1)] = loaded.to_dict()
        scheduler.add_job(added)
        scheduler._load_jobs_from_db = lambda: [loaded, cancelled]

        original_to_thread = module.asyncio.to_thread

        async def run_inline(func, *args, **kwargs):
            return func(*args, **kwargs)

        module.asyncio.to_thread = run_inline
        try:
            asyncio.run(scheduler.init())
        finally:
            module.asyncio.to_thread = original_to_thread

        self.assertEqual(set(scheduler.live), {(-1, 1), (-1, 2)})
        self.assertEqual(scheduler.chat_counts[-1], 2)

    def test_invalid_batch_item_does_not_abandon_valid_messages(self):
        module, sqlite = load_plugin()
        scheduler = module.AutoDeleteScheduler()
        bad = module.DeleteJob(100, -1, 1, "chat")
        good = module.DeleteJob(100, -1, 2, "chat")
        sqlite[module._job_key(-1, 1)] = bad.to_dict()
        sqlite[module._job_key(-1, 2)] = good.to_dict()

        class Client:
            def __init__(self):
                self.calls = []

            async def delete_messages(self, cid, mids):
                self.calls.append((cid, mids))
                if 1 in mids:
                    raise MessageIdInvalid()

        client = Client()
        asyncio.run(scheduler._process_batch(client, -1, [bad, good]))

        self.assertEqual(client.calls, [(-1, [1, 2]), (-1, [1]), (-1, [2])])
        self.assertNotIn(module._job_key(-1, 1), sqlite)
        self.assertNotIn(module._job_key(-1, 2), sqlite)


if __name__ == "__main__":
    unittest.main()
