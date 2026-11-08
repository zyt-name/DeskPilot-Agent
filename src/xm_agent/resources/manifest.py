from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Resource:
    id: str
    name: str
    version: str
    repo: str
    ref: str
    kind: str
    directory: str
    entry: str
    purpose: str
    runtime_checks: tuple[str, ...] = ()
    required: bool = True
    license: str = ""
    archive_url: str | None = None

    @property
    def archive(self) -> str:
        if self.archive_url:
            return self.archive_url
        return f"https://github.com/{self.repo}/archive/{self.ref}.zip"

    @property
    def dest(self) -> str:
        return f"vendor/{self.directory}-{self.version}"

    @property
    def link(self) -> str:
        return f"vendor/{self.directory}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "repo": self.repo,
            "ref": self.ref,
            "kind": self.kind,
            "dest": self.dest,
            "link": self.link,
            "entry": self.entry,
            "purpose": self.purpose,
            "required": self.required,
            "license": self.license,
            "upstream": f"https://github.com/{self.repo}",
        }


# 所有地址均在 2026-11-08 实测可用（PyPI / npm 返回 200，镜像归档返回 200 application/zip）。
GITHUB_ARCHIVE_MIRRORS: tuple[tuple[str, str], ...] = (
    ("ghproxy.net", "https://ghproxy.net/{url}"),
    ("gh-proxy.com", "https://gh-proxy.com/{url}"),
    ("ghfast.top", "https://ghfast.top/{url}"),
)

PYPI_INDEXES: tuple[tuple[str, str], ...] = (
    ("pypi", "https://pypi.org/simple"),
    ("tsinghua", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("aliyun", "https://mirrors.aliyun.com/pypi/simple"),
    ("tencent", "https://mirrors.cloud.tencent.com/pypi/simple"),
)

NPM_REGISTRIES: tuple[tuple[str, str], ...] = (
    ("npmjs", "https://registry.npmjs.org"),
    ("npmmirror", "https://registry.npmmirror.com"),
)


RESOURCES: tuple[Resource, ...] = (
    Resource(
        id="windows_mcp",
        name="Windows-MCP",
        version="0.8.8",
        repo="CursorTouch/Windows-MCP",
        ref="6613da7248cea1c22ac89ea9fdf74f11cfbecac8",
        kind="python",
        directory="Windows-MCP",
        entry=".venv/Scripts/python.exe",
        runtime_checks=("src/windows_mcp/__main__.py",),
        purpose="主桌面通道：Windows 原生 UI 树、截图、鼠标键盘与 PowerShell。",
        license="MIT",
    ),
    Resource(
        id="computer_use_mcp",
        name="Computer-Use MCP",
        version="1.8.0",
        repo="domdomegg/computer-use-mcp",
        ref="a41523056a4373984dc1faddbe757346fe480189",
        kind="node",
        directory="computer-use-mcp",
        entry="dist/main.js",
        runtime_checks=("node_modules/@modelcontextprotocol/sdk",),
        purpose="视觉通道：基于截图的坐标级桌面操作，需要支持图像输入的模型。",
        required=False,
        license="MIT",
    ),
    Resource(
        id="xshell_mcp",
        name="xshell-mcp",
        version="0.1.0",
        repo="2head1i/xshell-mcp",
        ref="709a5f59bc18a19feb8fb2237bef664d60717961",
        kind="node",
        directory="xshell-mcp",
        entry="dist/server.js",
        runtime_checks=("node_modules/@modelcontextprotocol/sdk",),
        purpose="终端通道：通过 Xshell 会话桥接脚本执行命令、读取屏幕与等待文本。",
        required=False,
        license="上游未声明（见仓库 README）",
    ),
)


def resource_by_id(resource_id: str) -> Resource | None:
    for resource in RESOURCES:
        if resource.id == resource_id:
            return resource
    return None
