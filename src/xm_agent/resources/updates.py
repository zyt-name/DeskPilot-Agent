"""资源更新检测：查上游新版本，判断能不能直接换。

只做「发现」和「兼容判断」，不做自动安装——换版本要过下载、构建、探针
三道关，任何一步失败都必须能退回旧版，这条链路单独实现（见 TECH_DESIGN
4.22.13）。这里的产出是「有个可用的新版本 / 但这个不能直接换」。

两个刻意的设计：

1. **不信任 GitHub 的 latest**：有的仓库 latest release 与代码 ref 不一致，
   所以版本号只用于「比大小」，真正的安装 ref 由调用方在换版本时确定。
2. **跨大版本一律判为不兼容**：MCP 协议与工具名在大版本间会变，
   Runtime 的提示词和审计规则都是按当前工具集写的，不能静默升级。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .manifest import Resource

GITHUB_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_TAGS = "https://api.github.com/repos/{repo}/tags"
CACHE_SECONDS = 6 * 60 * 60

Fetcher = Callable[[str], tuple[int, bytes]]


@dataclass
class UpdateCandidate:
    resource_id: str
    name: str
    current: str
    latest: str
    url: str = ""
    compatible: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.resource_id,
            "name": self.name,
            "current": self.current,
            "latest": self.latest,
            "url": self.url,
            "compatible": self.compatible,
            "reason": self.reason,
        }


def version_key(version: str) -> tuple[int, ...]:
    """把 `v1.2.3-beta` 这类版本化成可比较的元组。"""
    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


def _default_fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "xm-agent/0.1",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return int(getattr(response, "status", 200)), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


class UpdateChecker:
    """按仓库查最新版本，带内存缓存，避免把 GitHub 60 次/小时的额度打光。"""

    def __init__(
        self,
        fetcher: Fetcher | None = None,
        *,
        cache_seconds: int = CACHE_SECONDS,
    ) -> None:
        self.fetch = fetcher or _default_fetch
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, str, str]] = {}

    def latest(self, repo: str) -> tuple[str, str]:
        """返回 (版本, 链接)。查不到就返回空串，不抛异常。"""
        now = time.time()
        cached = self._cache.get(repo)
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1], cached[2]

        version, url = "", ""
        status, body = self.fetch(GITHUB_LATEST.format(repo=repo))
        if status == 200 and body:
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            version = str(data.get("tag_name") or data.get("name") or "")
            url = str(data.get("html_url") or "")
        if not version:
            status, body = self.fetch(GITHUB_TAGS.format(repo=repo))
            if status == 200 and body:
                try:
                    tags = json.loads(body)
                except ValueError:
                    tags = []
                names = [
                    str(item.get("name") or "")
                    for item in tags
                    if isinstance(item, dict)
                ]
                names = [name for name in names if name]
                if names:
                    version = max(names, key=version_key)

        self._cache[repo] = (now, version, url)
        return version, url

    def check(
        self, resources: Sequence[Resource], *, refresh: bool = False
    ) -> list[UpdateCandidate]:
        """refresh=True 会丢掉仓库缓存，强制重新问上游（用户点「刷新」时用）。"""
        if refresh:
            self._cache.clear()
        found: list[UpdateCandidate] = []
        for resource in resources:
            if not resource.repo:
                continue
            latest, url = self.latest(resource.repo)
            if not latest:
                continue
            if version_key(latest) <= version_key(resource.version):
                continue
            compatible, reason = self.compatible(resource, latest)
            found.append(
                UpdateCandidate(
                    resource_id=resource.id,
                    name=resource.name,
                    current=resource.version,
                    latest=latest,
                    url=url,
                    compatible=compatible,
                    reason=reason,
                )
            )
        return found

    @staticmethod
    def compatible(resource: Resource, latest: str) -> tuple[bool, str]:
        """跨大版本不自动换：工具集与提示词都按当前版本写的。"""
        if resource.kind == "browser":
            return False, "浏览器内核版本需与 CDP 命令集一起验证，只提示不自动升级"
        current = version_key(resource.version)
        target = version_key(latest)
        if current and target and current[0] != target[0]:
            return False, (
                f"跨大版本（{resource.version} → {latest}），"
                "工具集可能变化，需要人工确认"
            )
        return True, "同大版本，可通过探针后切换"
