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
    registered_listeners = []

    def fake_listener(*args, **kwargs):
        def decorator(func):
            registered_listeners.append((func.__name__, kwargs))
            return func
        return decorator

    listener_module.listener = fake_listener
    hook_module = types.ModuleType("pagermaid.hook")
    registered_hooks = {}

    def hook(event):
        def decorator(func):
            registered_hooks[event] = func
            return func
        return decorator

    hook_module.Hook = types.SimpleNamespace(
        load_success=lambda: hook("load_success"),
        reload_preprocessor=lambda: hook("reload_preprocessor"),
        on_shutdown=lambda: hook("on_shutdown"),
    )
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
        "pagermaid.hook": hook_module,
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
        module._test_registered_listeners = registered_listeners
        module._test_registered_hooks = registered_hooks
        return module, sqlite
    finally:
        for name, old_module in old_modules.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class AutoDeleteSchedulerTests(unittest.TestCase):
    def test_load_recovers_jobs_without_messages_and_reload_stops_old_worker(self):
        module, sqlite = load_plugin()

        async def check():
            deleted = asyncio.Event()
            calls = []

            class Client:
                async def delete_messages(self, cid, mids):
                    calls.append((cid, mids))
                    deleted.set()

            module.bot = Client()
            key = module._job_key(-1, 42)
            sqlite[key] = module.DeleteJob(0, -1, 42, "chat").to_dict()
            hooks = module._test_registered_hooks
            try:
                await hooks["load_success"]()
                old_worker = module.scheduler.worker_task
                await hooks["load_success"]()
                self.assertIs(module.scheduler.worker_task, old_worker)
                await asyncio.wait_for(deleted.wait(), timeout=2)
                self.assertEqual(calls, [(-1, [42])])
                self.assertNotIn(key, sqlite)

                future = module.DeleteJob(int(module.time.time()) + 3600, -1, 43, "chat")
                module.scheduler.add_job(future)
                await hooks["reload_preprocessor"]()
                self.assertTrue(old_worker.cancelled())
                self.assertIn(module._job_key(-1, 43), sqlite)
                # 热重载创建新调度器，旧 worker 必须已经停止。
                module.scheduler = module.AutoDeleteScheduler()
                await hooks["load_success"]()
                self.assertIsNot(module.scheduler.worker_task, old_worker)
            finally:
                await hooks["on_shutdown"]()
            self.assertTrue(module.scheduler.worker_task.cancelled())

        asyncio.run(check())

    def test_remote_reset_is_rejected_without_clearing_global_state(self):
        module, sqlite = load_plugin()
        sqlite[module._timer_key(0)] = 60
        edits = []

        class Message:
            arguments = "reset confirm -c -100123"
            chat = types.SimpleNamespace(id=-100999)

            async def edit(self, text):
                edits.append(text)

        asyncio.run(module.auto_del(Message()))
        self.assertEqual(sqlite[module._timer_key(0)], 60)
        self.assertIn("不能与 `-c` 同时使用", edits[-1])

    def test_invalid_persisted_job_is_removed_before_scheduling(self):
        module, sqlite = load_plugin()
        key = module._job_key(-1, 42)
        sqlite[key] = {
            "due_at": "bad",
            "source": "chat",
            "retry_count": 0,
            "flood_count": 0,
        }

        jobs = module.AutoDeleteScheduler()._load_jobs_from_db()
        self.assertEqual(jobs, [])
        self.assertNotIn(key, sqlite)

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


    def test_listener_handles_forwarded_and_via_bot_messages(self):
        module, _ = load_plugin()
        listeners_dict = dict(module._test_registered_listeners)
        self.assertIn("auto_del_task", listeners_dict)
        task_kwargs = listeners_dict["auto_del_task"]
        self.assertFalse(task_kwargs.get("incoming", True))
        self.assertTrue(task_kwargs.get("outgoing", False))
        self.assertFalse(task_kwargs.get("ignore_forwarded", True))
        self.assertFalse(task_kwargs.get("ignore_via_bot", True))


if __name__ == "__main__":
    unittest.main()
