"""`openai` SDK 的最小替身，供不需要真实调用的测试使用。

放在独立文件里，让 test_logic.py 与 test_agent_loop.py 共用同一套桩，
避免两处各自实现、行为不一致。

用法：
    import _openai_stub
    _openai_stub.install()                  # 在 import 应用代码之前调用
    _openai_stub.set_responder(fn)          # fn(kwargs) -> dict | Exception
    _openai_stub.set_client_factory(fn)     # 替换整个客户端（测鉴权失败等）
"""

from __future__ import annotations

import sys
import types
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 异常层级：与真实 SDK 的层次保持一致（AuthenticationError 属于 APIStatusError）
# ---------------------------------------------------------------------------


class APIError(Exception):
    pass


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


class APIStatusError(APIError):
    def __init__(self, message: str = "", status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class BadRequestError(APIStatusError):
    pass


# ---------------------------------------------------------------------------
# 响应对象：只需要支持 .model_dump()
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def model_dump(self) -> dict:
        return self._payload

    def __getitem__(self, key):
        return self._payload[key]

    def get(self, key, default=None):
        return self._payload.get(key, default)


def tool_call_response(name: str, arguments: dict | str, call_id: str = "call_1", content: str = "") -> dict:
    """构造一个"模型要求调用工具"的响应负载。"""
    import json as _json
    args = arguments if isinstance(arguments, str) else _json.dumps(arguments, ensure_ascii=False)
    return {
        "id": "chatcmpl-stub",
        "model": "stub-model",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }],
            },
        }],
    }


def final_response(content: str) -> dict:
    """构造一个"模型直接给答案"的响应负载。"""
    return {
        "id": "chatcmpl-stub",
        "model": "stub-model",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }


# ---------------------------------------------------------------------------
# 客户端替身
# ---------------------------------------------------------------------------

_responder: Callable[[dict], Any] | None = None
_client_factory: Callable[..., Any] | None = None
last_kwargs: dict | None = None


def set_responder(responder: Callable[[dict], Any] | None) -> None:
    """设置默认响应：responder(kwargs) 返回 dict（成功）或抛异常（失败）。"""
    global _responder
    _responder = responder


def set_client_factory(factory: Callable[..., Any] | None) -> None:
    """替换客户端构造（用于测"未配置密钥"等场景；传 None 恢复默认）。"""
    global _client_factory
    _client_factory = factory


def _default_create(**kwargs):
    global last_kwargs
    last_kwargs = kwargs
    if _responder is None:
        return _Response(final_response("stub: 没有设置 responder"))
    result = _responder(kwargs)
    if isinstance(result, BaseException):
        raise result
    return _Response(result)


class _Completions:
    create = staticmethod(_default_create)


class _Chat:
    completions = _Completions()


class FakeOpenAI:
    """替代 openai.OpenAI：记录构造参数，暴露 chat.completions.create。"""

    init_kwargs: dict | None = None

    def __init__(self, **kwargs):
        FakeOpenAI.init_kwargs = kwargs
        self.chat = _Chat()
        if _client_factory is not None:
            replacement = _client_factory(**kwargs)
            self.chat = replacement.chat


def install() -> types.ModuleType:
    """把替身装进 sys.modules（在 import 应用代码之前调用）。"""
    module = types.ModuleType("openai")
    module.OpenAI = FakeOpenAI
    module.APIError = APIError
    module.APIConnectionError = APIConnectionError
    module.APITimeoutError = APITimeoutError
    module.APIStatusError = APIStatusError
    module.AuthenticationError = AuthenticationError
    module.PermissionDeniedError = PermissionDeniedError
    module.RateLimitError = RateLimitError
    module.BadRequestError = BadRequestError
    sys.modules["openai"] = module
    return module
