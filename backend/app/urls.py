import re

# 独立的叶子模块：校验层（models）与抓取层（repo_service）共用同一套 URL 规则，
# 又不会形成 models -> repo_service -> config 的循环导入。
GITHUB_URL_PATTERN = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def is_github_repo_url(value: str) -> bool:
    return bool(GITHUB_URL_PATTERN.match((value or "").strip()))
