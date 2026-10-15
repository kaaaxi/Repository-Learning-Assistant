"""RepoAssistant 后端逻辑回归测试（无需安装 fastapi/openai/pydantic 等依赖）。

运行方式：
    python backend/tests/test_logic.py

脚本会在 import 应用代码之前，把第三方库替换成最小可用的替身，
因此可以直接在干净的解释器里运行，也适合作为 CI 的轻量检查。
"""

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 _openai_stub 可导入

import _openai_stub  # noqa: E402

# ---------------------------------------------------------------------------
# 第三方库替身（仅在测试进程内生效）
# ---------------------------------------------------------------------------


def _install_openai_stub() -> types.ModuleType:
    """openai SDK 的替身（实现在 _openai_stub.py，与其它测试共用）。

    请求层已从"直接 httpx.post"改为"openai SDK"，所以这里要伪装 SDK 本身：
      * OpenAI(...) 记录构造参数（用于断言 base_url / 超时 / 重试次数）；
      * client.chat.completions.create(**kwargs) 返回由 set_responder 控制的假响应；
      * 异常层级与真实 SDK 一致，方便测试 _describe_sdk_error 的分类。
    """
    return _openai_stub.install()


def _install_dotenv_stub() -> types.ModuleType:
    module = types.ModuleType("dotenv")
    module.load_dotenv = lambda *args, **kwargs: None
    return module


def _install_pydantic_stub() -> types.ModuleType:
    """只实现本项目用到的一点点能力：字段校验、校验失败报错、模型转字典。"""

    module = types.ModuleType("pydantic")

    class ValidationError(ValueError):
        def __init__(self, errors):
            super().__init__("validation error")
            self._errors = errors

        def errors(self):
            return self._errors

    def _matches(value, hint) -> bool:
        if hint is None or hint is type(None):
            return True
        if isinstance(hint, str):
            hint = _TYPES.get(hint)
        origin = getattr(hint, "__origin__", None)
        if origin in (list, dict):
            return isinstance(value, origin)
        if origin is not None:  # 联合类型，例如 str | None
            return any(_matches(value, arg) for arg in getattr(hint, "__args__", ()))
        return not isinstance(hint, type) or isinstance(value, hint)

    def _type_hint(hint, field):
        return field.get("annotation", hint)

    class _FieldInfo:
        def __init__(self, default=..., **kwargs):
            self.default = default
            self.kwargs = kwargs
            self.annotation = None

    def Field(default=..., **kwargs):
        return _FieldInfo(default, **kwargs)

    def field_validator(*names, **kwargs):
        def decorator(func):
            func.__validator_for__ = names
            return func

        return decorator

    def _unwrap_validator(value):
        """@field_validator 通常套在 @classmethod 上，这里取出可调用的底层函数。"""
        return value.__func__ if isinstance(value, classmethod) else value

    class _Meta(type):
        def __new__(mcls, name, bases, namespace):
            cls = super().__new__(mcls, name, bases, namespace)
            # Python 3.14 起注解是惰性求值的（PEP 649）：建类时命名空间里没有 __annotations__，
            # 必须等类建好之后再读 cls.__annotations__。
            annotations = dict(getattr(cls, "__annotations__", {}) or {})
            fields = {}
            validators = {}
            for base in bases:
                fields.update(getattr(base, "_fields", {}))
                validators.update(getattr(base, "_validators", {}))
            for field_name, hint in annotations.items():
                value = namespace.get(field_name, ...)
                if isinstance(value, _FieldInfo):
                    # 忽略 Field(...) 里"必填"的占位默认值
                    default = None if value.default is ... else value.default
                    constraints = value.kwargs
                else:
                    default = value
                    constraints = {}
                fields[field_name] = (hint, default, constraints)
            for value in list(namespace.values()):
                for target in getattr(value, "__validator_for__", ()):
                    validators[target] = _unwrap_validator(value)
            cls._fields = fields
            cls._validators = validators
            return cls

    class BaseModel(metaclass=_Meta):
        def __init__(self, **data):
            values = {}
            errors = []
            for field_name, (hint, default, constraints) in self._fields.items():
                if field_name in data:
                    value = data[field_name]
                elif default is not None:
                    value = default
                else:
                    errors.append({"loc": (field_name,), "msg": "field required"})
                    continue
                if value is not None and not _matches(value, hint):
                    errors.append({"loc": (field_name,), "msg": f"expected {hint}"})
                    continue
                if isinstance(value, str):
                    min_length = constraints.get("min_length")
                    max_length = constraints.get("max_length")
                    if min_length is not None and len(value) < min_length:
                        errors.append({"loc": (field_name,), "msg": f"shorter than min_length={min_length}"})
                        continue
                    if max_length is not None and len(value) > max_length:
                        errors.append({"loc": (field_name,), "msg": f"longer than max_length={max_length}"})
                        continue
                values[field_name] = value
            if errors:
                raise ValidationError(errors)
            for field_name, validator in self._validators.items():
                if field_name in values:
                    try:
                        values[field_name] = validator(self, values[field_name])
                    except ValueError as exc:  # 与真实 pydantic 一致：校验器里的 ValueError 即校验失败
                        raise ValidationError([{"loc": (field_name,), "msg": str(exc)}]) from exc
            self.__dict__.update(values)

        @classmethod
        def model_validate(cls, payload):
            return cls(**payload)

        def model_dump(self):
            return {name: getattr(self, name) for name in self._fields}

    module.BaseModel = BaseModel
    module.Field = Field
    module.field_validator = field_validator
    module.ValidationError = ValidationError
    module.HttpUrl = str
    return module


def _install_fastapi_stub() -> tuple[types.ModuleType, types.ModuleType]:
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class FastAPI:
        def __init__(self, **kwargs):
            self.routes = {}

        def add_middleware(self, *args, **kwargs):
            pass

        def get(self, path, **kwargs):
            return lambda func: self.routes.setdefault(("GET", path), func)

        def post(self, path, **kwargs):
            return lambda func: self.routes.setdefault(("POST", path), func)

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException

    middleware = types.ModuleType("fastapi.middleware")
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = type("CORSMiddleware", (), {"__init__": lambda self, **kw: None})
    return fastapi, cors


_TYPES = {"str": str, "int": int, "bool": bool, "float": float, "list": list, "dict": dict}


def _install_stubs(secrets_file: Path) -> None:
    os.environ["REPOASSISTANT_SECRETS_FILE"] = str(secrets_file)
    os.environ.pop("DEEPSEEK_API_KEY", None)
    sys.modules["openai"] = _install_openai_stub()
    sys.modules["dotenv"] = _install_dotenv_stub()
    sys.modules["pydantic"] = _install_pydantic_stub()
    fastapi, cors = _install_fastapi_stub()
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.middleware"] = types.ModuleType("fastapi.middleware")
    sys.modules["fastapi.middleware.cors"] = cors


# ---------------------------------------------------------------------------
# 极简测试框架
# ---------------------------------------------------------------------------

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def check_eq(name: str, actual, expected) -> None:
    check(name, actual == expected, f"期望 {expected!r}，实际 {actual!r}")


def check_raises(name: str, exc_types, func) -> None:
    try:
        func()
    except exc_types as exc:
        check(name, True)
        return
    except Exception as exc:  # 类型不符时把真实异常打出来，便于定位
        check(name, False, f"抛出的是 {type(exc).__name__}: {exc}")
        return
    check(name, False, "没有报错")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 测试主体
# ---------------------------------------------------------------------------


def _make_workdir() -> Path:
    """优先用系统临时目录；沙箱只允许写工作区时回退到仓库根的 .tmp-tests。

    注意：这里刻意不用 tempfile.mkdtemp 的默认权限（0o700），
    否则在受限沙箱里连自己创建的目录都进不去。
    """
    try:
        probe = Path(tempfile.gettempdir()) / f"repoassistant-test-{os.getpid()}"
        probe.mkdir(mode=0o777, parents=True, exist_ok=True)
        (probe / ".probe").write_text("x", encoding="utf-8")
        (probe / ".probe").unlink()
        return probe
    except OSError:
        fallback = Path(__file__).resolve().parents[2] / ".tmp-tests"
        fallback.mkdir(mode=0o777, parents=True, exist_ok=True)
        sandbox_dir = fallback / f"run-{os.getpid()}"
        sandbox_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
        return sandbox_dir


def main() -> int:
    workdir = _make_workdir()
    secrets_file = workdir / "secrets.json"
    secrets_file.write_text(
        json.dumps({"DEEPSEEK_API_KEY": "sk-test-key-for-unit-tests"}), encoding="utf-8"
    )

    _install_stubs(secrets_file)

    from app import config
    from app.services import analysis_service as asvc
    from app.services import llm_service, repo_service, storage_service
    from app.urls import is_github_repo_url

    config.REPOS_DIR = workdir / "repos"
    config.ANALYSES_DIR = workdir / "analyses"
    storage_service.ANALYSES_DIR = config.ANALYSES_DIR
    config.REPOS_DIR.mkdir(parents=True, exist_ok=True)
    config.ANALYSES_DIR.mkdir(parents=True, exist_ok=True)

    # --- 1. 密钥加载（#1 不明示） -------------------------------------------
    section("1. 密钥加载")
    check_eq("secrets.json 中的密钥被读取", config.DEEPSEEK_API_KEY, "sk-test-key-for-unit-tests")
    check_eq("来源标记为 file", config.API_KEY_SOURCE, "file")
    check("配置了密钥时不再处于 Demo 模式", config.DEMO_MODE is False)
    check("密钥提示串不含密钥内容", "sk-test" not in config.api_key_hint(), config.api_key_hint())

    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    reloaded_key, reloaded_source = config._resolve_api_key()
    check_eq("环境变量优先于文件", (reloaded_key, reloaded_source), ("sk-from-env", "env"))
    os.environ.pop("DEEPSEEK_API_KEY")

    # --- 2. URL 规范与匿名 ID ----------------------------------------------
    section("2. URL 校验与缓存键")
    check_eq(
        "三种写法归一到同一 analysis_id",
        len({repo_service.generate_analysis_id(u) for u in (
            "https://github.com/pallets/click",
            "https://github.com/pallets/click/",
            "https://github.com/pallets/click.git",
        )}),
        1,
    )
    check("非 GitHub 地址被拒绝", not is_github_repo_url("https://gitlab.com/a/b"))
    check("owner/repo 之外的路径被拒绝", not is_github_repo_url("https://github.com/a/b/issues"))

    # --- 3. 目录树解析（#2 嵌套目录） ---------------------------------------
    section("3. 目录树深度解析")
    tree_text = "\n".join([
        "├── backend/",
        "│   ├── app/",
        "│   │   └── services/",
        "│   │       └── repo_service.py",
        "│   └── Dockerfile",
        "├── frontend/",
        "│   └── src/",
        "│       └── App.vue",
        "└── README.md",
    ])
    dirs, files = asvc.extract_top_dirs(tree_text)
    check_eq("只识别顶层目录", dirs, ["backend", "frontend"])
    check_eq("识别根目录文件", files, ["README.md"])
    check_eq(
        "深层目录不再被当成顶层目录",
        "services" in dirs or "app" in dirs or "src" in dirs,
        False,
    )

    # --- 4. 源码抽样与关键文件（#2 真实路径） --------------------------------
    section("4. 源码抽样与关键文件")
    repo = workdir / "fake-repo"
    (repo / "backend" / "app" / "services").mkdir(parents=True)
    (repo / "frontend" / "src").mkdir(parents=True)
    (repo / "backend" / "app" / "main.py").write_text("print('main')\n", encoding="utf-8")
    (repo / "backend" / "app" / "services" / "svc.py").write_text("print('svc')\n", encoding="utf-8")
    (repo / "frontend" / "src" / "App.vue").write_text("<template/>\n", encoding="utf-8")

    snippets = asvc._source_snippets(repo)
    snippet_names = [s.splitlines()[0].strip("- ").strip() for s in snippets]
    check_eq(
        "抽样用真实相对路径且覆盖两个顶层目录",
        sorted(snippet_names),
        ["backend/app/main.py", "backend/app/services/svc.py", "frontend/src/App.vue"],
    )

    key_files = asvc._collect_key_files_by_dir(repo, ["backend", "frontend"])
    check_eq(
        "关键文件是真实的相对路径",
        key_files["backend"],
        ["app/main.py", "app/services/svc.py"],
    )

    # --- 5. monorepo 配置识别（#8） ----------------------------------------
    section("5. 配置识别范围")
    monorepo = workdir / "monorepo"
    (monorepo / "packages" / "web").mkdir(parents=True)
    (monorepo / "packages" / "web" / "package.json").write_text('{"name":"web"}', encoding="utf-8")
    (monorepo / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    configs = repo_service.extract_config_files(monorepo)
    check("根目录配置被识别", "requirements.txt" in configs)
    check("子目录清单被识别", "packages/web/package.json" in configs)

    # --- 6. 目录树截断顺序（#8） -------------------------------------------
    section("6. 目录树：先过滤再截断")
    crowded = workdir / "crowded"
    crowded.mkdir()
    for i in range(80):
        (crowded / f"file{i:02d}.txt").write_text("x", encoding="utf-8")
    (crowded / "node_modules").mkdir()
    built = repo_service.build_directory_tree(crowded)
    check_eq("每层最多 50 个条目", len(built), 50)
    check("被忽略目录不出现在树里", "node_modules/" not in built)

    # --- 7. README 清洗（#8 括号链接） --------------------------------------
    section("7. README 清洗")
    cleaned = asvc.clean_readme_overview(
        "看 [文档](https://en.wikipedia.org/wiki/Foo_(bar)) 和 ![图](a.png)。"
    )
    check("带括号的链接被完整替换", cleaned == "看 文档 和 。", repr(cleaned))

    # --- 8. base_url 归一（#5） --------------------------------------------
    section("8. LLM 端点与 base_url 归一")
    check_eq("默认地址", llm_service.chat_completions_url("https://api.deepseek.com"),
             "https://api.deepseek.com/v1/chat/completions")
    check_eq("已带 /v1", llm_service.chat_completions_url("https://api.deepseek.com/v1"),
             "https://api.deepseek.com/v1/chat/completions")
    check_eq("带尾斜杠的 /v1/", llm_service.chat_completions_url("https://api.deepseek.com/v1/"),
             "https://api.deepseek.com/v1/chat/completions")
    check_eq("空值回退到默认", llm_service.chat_completions_url(""),
             "https://api.deepseek.com/v1/chat/completions")

    # SDK 会在 base_url 之后自动拼 /chat/completions，所以传给 SDK 的必须带 /v1
    check_eq("SDK 用的 base_url 带 /v1", llm_service.sdk_base_url("https://api.deepseek.com"),
             "https://api.deepseek.com/v1")
    check_eq("SDK 用的 base_url 不重复拼 /v1", llm_service.sdk_base_url("https://api.deepseek.com/v1/"),
             "https://api.deepseek.com/v1")
    check_eq("SDK 用的 base_url 空值回退", llm_service.sdk_base_url(""),
             "https://api.deepseek.com/v1")
    check_eq("SDK base_url + 路径 == 完整端点",
             llm_service.sdk_base_url("https://api.deepseek.com") + "/chat/completions",
             llm_service.chat_completions_url("https://api.deepseek.com"))

    # --- 9. LLM 失败不再静默（#3） ------------------------------------------
    section("9. LLM 失败可见")
    ctx = {
        "analysis_id": "abc123abc123",
        "project_name": "fake-repo",
        "repo_path": str(repo),
        "readme": "# demo",
        "directory_tree": tree_text,
        "config_files": {"requirements.txt": "fastapi\n"},
    }

    # 网络不通
    _openai_stub.set_responder(lambda kwargs: _openai_stub.APIConnectionError("connection refused"))
    result, error = llm_service.analyze_with_llm({**ctx, "sample_sources": snippets})
    check_eq("网络失败返回 None 结果", result, None)
    check("网络失败返回可读原因（含『网络』字样）",
          bool(error) and "connection refused" in error and "网络" in error, str(error))

    # 鉴权失败：应给出可执行提示，而不是只报状态码
    _openai_stub.set_responder(lambda kwargs: _openai_stub.AuthenticationError("401 Unauthorized", status_code=401))
    result, error = llm_service.analyze_with_llm({**ctx, "sample_sources": snippets})
    check("鉴权失败被分类并给出可执行提示",
          bool(error) and "鉴权失败" in error and "401" in error, str(error))

    # 限流
    _openai_stub.set_responder(lambda kwargs: _openai_stub.RateLimitError("429 Too Many Requests", status_code=429))
    result, error = llm_service.analyze_with_llm({**ctx, "sample_sources": snippets})
    check("限流被分类", bool(error) and "限流" in error, str(error))

    # 模型名/参数被拒
    _openai_stub.set_responder(lambda kwargs: _openai_stub.BadRequestError("model not found", status_code=400))
    result, error = llm_service.analyze_with_llm({**ctx, "sample_sources": snippets})
    check("400 提示多半是模型名或参数问题", bool(error) and "400" in error, str(error))

    # 服务端错误
    _openai_stub.set_responder(lambda kwargs: _openai_stub.APIStatusError("500 Internal", status_code=500))
    result, error = llm_service.analyze_with_llm({**ctx, "sample_sources": snippets})
    check("5xx 提示稍后重试", bool(error) and "服务端错误" in error, str(error))

    # 未配置密钥：不应抛异常，而是返回可读原因
    original_resolve = config._resolve_api_key
    config._resolve_api_key = lambda: ("", "none")
    llm_service.DEEPSEEK_API_KEY = ""
    result, error = llm_service.request_chat_completion([{"role": "user", "content": "hi"}])
    check("未配置密钥时返回可读原因而不抛异常",
          result is None and bool(error) and "未配置" in error, str(error))
    config._resolve_api_key = original_resolve
    llm_service.DEEPSEEK_API_KEY = "sk-test-key-for-unit-tests"

    _openai_stub.set_responder(None)

    # --- 9b. 输出被截断时的补全 ---------------------------------------------
    section("9b. LLM 输出截断修复")
    check("断在值中间：丢掉半个键值对并保留前面的字段",
          llm_service._repair_truncated_json('{"overview": "说明", "code_map": [{"name": "src", "desc')
          == {"overview": "说明", "code_map": [{"name": "src"}]},
          repr(llm_service._repair_truncated_json('{"overview": "说明", "code_map": [{"name": "src", "desc')))
    check("断在字符串值里：补全引号",
          llm_service._repair_truncated_json('{"overview": "被截断的概述", "architecture_summary": "前端使用 Vu')
          == {"overview": "被截断的概述", "architecture_summary": "前端使用 Vu"},
          repr(llm_service._repair_truncated_json('{"overview": "被截断的概述", "architecture_summary": "前端使用 Vu')))
    check("多个未闭合层级被补全",
          llm_service._repair_truncated_json('{"a": 1, "b": {"c": [1, 2') == {"a": 1, "b": {"c": [1, 2]}},
          repr(llm_service._repair_truncated_json('{"a": 1, "b": {"c": [1, 2')))
    check("断在数组元素中间：补全可见内容",
          llm_service._repair_truncated_json('{"a": 1, "list": ["x", "y') == {"a": 1, "list": ["x", "y"]},
          repr(llm_service._repair_truncated_json('{"a": 1, "list": ["x", "y')))
    check("转义引号不会截错",
          llm_service._repair_truncated_json(r'{"a": "带\"引号", "b": "未完成') == {"a": '带"引号', "b": "未完成"},
          repr(llm_service._repair_truncated_json(r'{"a": "带\"引号", "b": "未完成')))
    check("转义符被截半时不强行补全该值",
          llm_service._repair_truncated_json('{"a": "ok", "b": "half\\') is None
          or isinstance(llm_service._repair_truncated_json('{"a": "ok", "b": "half\\'), dict),
          repr(llm_service._repair_truncated_json('{"a": "ok", "b": "half\\')))
    check("转义序列被截半时保留前面的完整字段",
          llm_service._repair_truncated_json('{"a": "ok", "b": "half\\n') == {"a": "ok", "b": "half\n"},
          repr(llm_service._repair_truncated_json('{"a": "ok", "b": "half\\n')))
    check("完整 JSON 也能解析", llm_service._repair_truncated_json('{"a": 1}') == {"a": 1}, "")
    check("彻底损坏的内容返回 None", llm_service._repair_truncated_json("这不是 JSON") is None, "")

    _openai_stub.set_responder(lambda kwargs: {
        "choices": [{"message": {"content": '{"overview": "被截断的概述", "code_map": [{"name": "src"'}}]
    })
    result, error = llm_service.analyze_with_llm({**ctx, "sample_sources": snippets})
    check("截断输出被修复后不再回退", isinstance(result, dict) and result.get("overview") == "被截断的概述" and error is None,
          f"result={result!r} error={error!r}")
    _openai_stub.set_responder(None)

    # --- 10. 缓存语义（#4） ------------------------------------------------
    section("10. 缓存语义")
    config.DEMO_MODE = False
    config.ANALYSIS_CACHE_TTL = 3600
    asvc.ANALYSIS_CACHE_TTL = 3600
    check("新鲜缓存命中", asvc._cache_is_fresh({"analyzed_at": asvc.datetime.now(asvc.timezone.utc).isoformat()}))
    check("过期缓存不命中", not asvc._cache_is_fresh({"analyzed_at": "2020-01-01T00:00:00+00:00"}))
    check("缺少时间戳的历史缓存视为过期", not asvc._cache_is_fresh({}))
    asvc.ANALYSIS_CACHE_TTL = 0
    check("TTL=0 时永不过期", asvc._cache_is_fresh({"analyzed_at": "2020-01-01T00:00:00+00:00"}))
    asvc.ANALYSIS_CACHE_TTL = 3600

    # --- 11. 端到端：分析 + 问答回退 ---------------------------------------
    section("11. 端到端（规则模式）")
    # DEMO_MODE 在模块导入时就被绑定成常量，所以两个模块都要改
    config.DEMO_MODE = True
    llm_service.DEMO_MODE = True
    asvc.DEMO_MODE = True
    asvc.ANALYSIS_CACHE_TTL = config.ANALYSIS_CACHE_TTL

    original_extract = repo_service.extract_repo_context
    extract_calls = {"n": 0}

    def fake_extract(url, aid):
        extract_calls["n"] += 1
        return {
            "analysis_id": aid,
            "project_name": "fake-repo",
            "owner": "owner",
            "repo_path": str(repo),
            "readme": "# Fake\n\n这是测试仓库。",
            "directory_tree": tree_text,
            "config_files": {"requirements.txt": "fastapi\n"},
        }

    repo_service.extract_repo_context = fake_extract
    try:
        first = asvc.analyze("https://github.com/owner/fake-repo")
        check_eq("顶层目录进入 code_map", [c["name"] for c in first["code_map"]][:2], ["backend", "frontend"])
        check(
            "关键模块带有真实文件路径",
            first["key_modules"][0]["key_files"] == ["app/main.py", "app/services/svc.py"],
            str(first["key_modules"][0]["key_files"]),
        )
        check("结果带分析时间戳", bool(first.get("analyzed_at")))
        check_eq("Demo 模式标记", first["analysis_mode"], "demo")

        second = asvc.analyze("https://github.com/owner/fake-repo")
        check_eq("Demo 模式复用缓存", second["analyzed_at"], first["analyzed_at"])
        check_eq("Demo 模式没有重复分析", extract_calls["n"], 1)

        config.DEMO_MODE = False
        llm_service.DEMO_MODE = False
        asvc.DEMO_MODE = False
        third = asvc.analyze("https://github.com/owner/fake-repo")
        check(
            "有密钥时会重新分析而不是复用规则缓存",
            extract_calls["n"] == 2 and third.get("analysis_mode") == "demo",
            f"extract 调用 {extract_calls['n']} 次, mode={third.get('analysis_mode')}",
        )
        check("重新分析的结果仍带 LLM 失败原因", bool(third.get("llm_error")), str(third.get("llm_error")))
        config.DEMO_MODE = True
        llm_service.DEMO_MODE = True
        asvc.DEMO_MODE = True
    finally:
        repo_service.extract_repo_context = original_extract

    # --- 12. 问答回退（#3） ------------------------------------------------
    section("12. 问答回退可见")
    analysis = storage_service.load_analysis(repo_service.generate_analysis_id("https://github.com/owner/fake-repo"))
    fallback = asvc.chat_fallback(analysis, "这个项目是做什么的？", reason="HTTP 401 Unauthorized")
    check_eq("回退来源标记", fallback["source"], "fallback")
    check("回退回答里带上失败原因", "HTTP 401 Unauthorized" in fallback["answer"])
    asvc.DEMO_MODE = True
    demo_answer = asvc.chat_fallback(analysis, "这个项目是做什么的？")
    check("Demo 模式不显示错误", "LLM 调用失败" not in demo_answer["answer"])
    check("Demo 模式仍给出提示", "Demo 模式" in demo_answer["answer"])
    # Demo 模式不是失败：chat_with_llm 必须返回"没有错误原因"，否则前端会把 Demo 当成 LLM 故障
    llm_service.DEMO_MODE = True
    answer, reason = llm_service.chat_with_llm(analysis, "这个项目是做什么的？")
    check("Demo 模式下 chat_with_llm 不报错误原因", answer is None and reason is None, f"answer={answer!r} reason={reason!r}")

    # --- 13. 路径穿越防护（#8） --------------------------------------------
    section("13. analysis_id 校验")
    storage_service.ANALYSES_DIR = config.ANALYSES_DIR
    for bad in ["../secrets", "..\\secrets", "/etc/passwd", "a/b", ""]:
        try:
            storage_service.load_analysis(bad)
            check(f"拒绝非法 analysis_id {bad!r}", False, "没有报错")
        except storage_service.UnsafeAnalysisId:
            check(f"拒绝非法 analysis_id {bad!r}", True)
        except FileNotFoundError:
            check(f"拒绝非法 analysis_id {bad!r}", False, "被当成普通文件查找")
    storage_service.save_analysis("deadbeef1234", {"ok": True})
    check_eq("合法 id 可正常读写", storage_service.load_analysis("deadbeef1234"), {"ok": True})

    # --- 14. 请求校验（#8） -------------------------------------------------
    section("14. 请求模型校验")
    pydantic_stub = sys.modules["pydantic"]
    from app.models import AnalyzeRequest, ChatRequest

    ok = AnalyzeRequest(repo_url=" https://github.com/pallets/click ")
    check_eq("repo_url 被去空格", ok.repo_url, "https://github.com/pallets/click")
    check_eq("force_refresh 默认关闭", ok.force_refresh, False)
    check_raises(
        "非 GitHub 地址在入口被拒",
        pydantic_stub.ValidationError,
        lambda: AnalyzeRequest(repo_url="https://example.com/not-github"),
    )
    check_raises(
        "空问题在入口被拒",
        pydantic_stub.ValidationError,
        lambda: ChatRequest(analysis_id="x", question=""),
    )
    check("github 地址可通过", AnalyzeRequest(repo_url="https://github.com/a/b.git").repo_url.endswith(".git"))

    # --- 15. API 路由与健康检查（#7） --------------------------------------
    section("15. 健康检查不泄露密钥")
    from app import main as app_main

    health = app_main.health()
    check_eq("健康检查字段", sorted(health), ["api_key_source", "cache_ttl_seconds", "demo_mode", "model", "status"])
    check("健康检查不含密钥内容", "sk-" not in json.dumps(health), json.dumps(health, ensure_ascii=False))
    check("分析接口返回结构体已声明", ("POST", "/api/analyze") in app_main.app.routes)
    check_eq("非法仓库地址返回 400", _status_of(app_main, "https://example.com/x"), 400)

    # --- 16. 首次运行生成模板（#1） ----------------------------------------
    section("16. secrets.json 模板")
    fresh = workdir / "fresh-secrets.json"
    original_secrets = config.SECRETS_FILE
    config.SECRETS_FILE = fresh
    config.ensure_secrets_template()
    template = json.loads(fresh.read_text(encoding="utf-8"))
    check_eq("模板密钥为空", template["DEEPSEEK_API_KEY"], "")
    check("模板含中文说明", bool(template["_readme"]))
    config.SECRETS_FILE = original_secrets

    print("\n" + "=" * 60)
    print(f"通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    if _FAILED:
        for name, detail in _FAILED:
            print(f"  - {name}: {detail}")
        return 1
    return 0


def _status_of(app_main, repo_url: str) -> int:
    try:
        app_main.analyze(type("Req", (), {"repo_url": repo_url, "force_refresh": False})())
        return 200
    except Exception as exc:  # HTTPException 替身
        return getattr(exc, "status_code", 500)


if __name__ == "__main__":
    raise SystemExit(main())
