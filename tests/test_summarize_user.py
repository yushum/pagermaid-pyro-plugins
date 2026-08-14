import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


class FakeSqlite(dict):
    pass


class FakeRequest:
    def __init__(self, method, url):
        self.method = method
        self.url = url


class FakeHttpStatusError(Exception):
    def __init__(self, message, request=None, response=None):
        super().__init__(message)
        self.request = request
        self.response = response


class FakeRequestError(Exception):
    pass


class FakeTimeoutException(FakeRequestError):
    pass


class FakeResponse:
    _unset = object()

    def __init__(self, status_code, request=None, text=None, json_data=_unset):
        self.status_code = status_code
        self.request = request
        self.text = text if text is not None else ""
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHttpStatusError(
                f"HTTP {self.status_code}", request=self.request, response=self
            )

    def json(self):
        if self._json_data is not self._unset:
            return self._json_data
        return json.loads(self.text)


class StubAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def aclose(self):
        pass


def load_plugin():
    sqlite = FakeSqlite()

    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = StubAsyncClient
    httpx.Timeout = lambda *args, **kwargs: object()
    httpx.Limits = lambda *args, **kwargs: object()
    httpx.Request = FakeRequest
    httpx.Response = FakeResponse
    httpx.HTTPStatusError = FakeHttpStatusError
    httpx.RequestError = FakeRequestError
    httpx.TimeoutException = FakeTimeoutException

    pyrogram = types.ModuleType("pyrogram")
    pyrogram_enums = types.ModuleType("pyrogram.enums")
    pyrogram_enums.ChatType = types.SimpleNamespace(PRIVATE="private", BOT="bot")
    pyrogram_enums.ParseMode = types.SimpleNamespace(HTML="html", DISABLED=None)
    pyrogram.enums = pyrogram_enums

    pagermaid = types.ModuleType("pagermaid")
    services = types.ModuleType("pagermaid.services")
    services.sqlite = sqlite
    enums = types.ModuleType("pagermaid.enums")
    enums.Client = object
    enums.Message = object
    listener_module = types.ModuleType("pagermaid.listener")
    listener_module.listener = lambda *args, **kwargs: lambda func: func

    modules = {
        "httpx": httpx,
        "pyrogram": pyrogram,
        "pyrogram.enums": pyrogram_enums,
        "pagermaid": pagermaid,
        "pagermaid.services": services,
        "pagermaid.enums": enums,
        "pagermaid.listener": listener_module,
    }
    old_modules = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        path = Path(__file__).parents[1] / "summarize_user" / "main.py"
        spec = importlib.util.spec_from_file_location("summarize_user_under_test", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old_module in old_modules.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class FakeHttpClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if self.error:
            raise self.error
        return self.response


class SummarizeUserHelpersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.module._http_client.aclose())

    def test_build_chat_completions_url_accepts_base_or_full_endpoint(self):
        build = self.module._build_chat_completions_url
        self.assertEqual(
            build("https://example.com/v1/"),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            build(" https://example.com/v1/chat/completions/ "),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            build("https://example.com/v1?token=abc"),
            "https://example.com/v1/chat/completions?token=abc",
        )

    def test_extracts_openai_string_and_text_parts(self):
        extract = self.module._extract_content
        self.assertEqual(
            extract({"choices": [{"message": {"content": " answer "}}]}),
            "answer",
        )
        self.assertEqual(
            extract(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "one"},
                                    {"type": "output_text", "text": "two"},
                                ]
                            }
                        }
                    ]
                }
            ),
            "one\ntwo",
        )

    def test_does_not_treat_reasoning_or_native_response_as_final_answer(self):
        extract = self.module._extract_content
        self.assertIsNone(
            extract(
                {
                    "choices": [
                        {"message": {"content": "", "reasoning_content": "private"}}
                    ]
                }
            )
        )
        self.assertIsNone(extract({"response": "ollama native response"}))
        self.assertIsNone(
            extract({"candidates": [{"content": None}]})
        )

    def test_preview_is_json_formatted_truncated_and_redacted(self):
        preview = self.module._response_preview(
            {"error": "secret-value", "token": "sk-test-secret"},
            api_key="secret-value",
            max_chars=60,
        )
        self.assertNotIn("secret-value", preview)
        self.assertNotIn("sk-test-secret", preview)
        self.assertIn("****", preview)


class SummarizeUserCallTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()
        cls.original_client = cls.module._http_client

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.original_client.aclose())

    async def asyncTearDown(self):
        self.module._http_client = self.original_client

    async def call_llm(self):
        return await self.module._call_llm(
            "user text",
            "system prompt",
            "secret-api-key",
            "https://example.com/v1",
            "test-model",
        )

    async def test_successful_openai_response(self):
        request = FakeRequest("POST", "https://example.com/v1/chat/completions")
        response = FakeResponse(
            200,
            request=request,
            json_data={"choices": [{"message": {"content": "done"}}]},
        )
        fake_client = FakeHttpClient(response=response)
        self.module._http_client = fake_client

        self.assertEqual(await self.call_llm(), "done")
        self.assertEqual(
            fake_client.calls[0][0], "https://example.com/v1/chat/completions"
        )

    async def test_http_error_includes_redacted_response_preview(self):
        request = FakeRequest("POST", "https://example.com/v1/chat/completions")
        response = FakeResponse(
            401,
            request=request,
            text="bad key: secret-api-key <invalid>",
        )
        self.module._http_client = FakeHttpClient(response=response)

        with self.assertRaisesRegex(Exception, "HTTP 401") as raised:
            await self.call_llm()
        self.assertIn("<invalid>", str(raised.exception))
        self.assertNotIn("secret-api-key", str(raised.exception))

    async def test_invalid_json_includes_actual_response_preview(self):
        request = FakeRequest("POST", "https://example.com/v1/chat/completions")
        response = FakeResponse(200, request=request, text="upstream HTML error")
        self.module._http_client = FakeHttpClient(response=response)

        with self.assertRaisesRegex(Exception, "非 JSON") as raised:
            await self.call_llm()
        self.assertIn("upstream HTML error", str(raised.exception))

    async def test_unrecognized_json_includes_actual_response_preview(self):
        request = FakeRequest("POST", "https://example.com/v1/chat/completions")
        response = FakeResponse(
            200, request=request, json_data={"unexpected": "value"}
        )
        self.module._http_client = FakeHttpClient(response=response)

        with self.assertRaisesRegex(Exception, "无法识别") as raised:
            await self.call_llm()
        self.assertIn('{"unexpected":"value"}', str(raised.exception))

    async def test_http_200_error_object_is_reported(self):
        request = FakeRequest("POST", "https://example.com/v1/chat/completions")
        response = FakeResponse(
            200,
            request=request,
            json_data={
                "error": {
                    "message": "model unavailable",
                    "type": "server_error",
                    "code": "overloaded",
                }
            },
        )
        self.module._http_client = FakeHttpClient(response=response)

        with self.assertRaisesRegex(Exception, "model unavailable") as raised:
            await self.call_llm()
        self.assertIn("server_error/overloaded", str(raised.exception))

    async def test_all_map_failures_preserve_first_api_error(self):
        original_chunk_texts = self.module._chunk_texts
        original_call_llm = self.module._call_llm
        self.module._chunk_texts = lambda texts: ["chunk one", "chunk two"]

        async def fail_call(*args, **kwargs):
            raise Exception("API 返回 HTTP 429。实际响应预览：rate limited")

        self.module._call_llm = fail_call
        try:
            with self.assertRaisesRegex(Exception, "HTTP 429") as raised:
                await self.module._map_reduce_summary(
                    ["message"],
                    "secret-api-key",
                    "https://example.com/v1",
                    "test-model",
                )
            self.assertIn("rate limited", str(raised.exception))
        finally:
            self.module._chunk_texts = original_chunk_texts
            self.module._call_llm = original_call_llm


if __name__ == "__main__":
    unittest.main()
