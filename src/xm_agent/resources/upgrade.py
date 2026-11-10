"""资源升级链路：旁路安装 → 探针 → 切换 → 失败回滚。

四条硬规矩，改之前先看 TECH_DESIGN 4.22.15：

1. **绝不覆盖在用的目录**。新版本装进 `<install_dir>/<dir>-<version>`，
   稳定入口（`vendor/Windows-MCP`、`engines/chromium`）在探针通过前一律不动。
2. **探针说了算**。文件存在、依赖装完都不算数，必须真的把 MCP 服务拉起来握手、
   或把内核拉起来读版本；探不通就当作坏的。
3. **失败必须能退回旧版**。任何一步抛错都删掉旁路目录、保持旧链接，
   并在结果里写清失败阶段与原因。
4. **同一版本失败后要有冷却**。否则面板每十分钟检查一次就重下一次 200MB。

版本来源分两类：MCP 资源按 GitHub Release/Tag（`ref` 用原始 tag，`version` 归一成
纯数字），浏览器内核按 Chrome for Testing 的版本号（下载模板里的 `{version}`）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .manifest import Resource, clean_version
from .updates import UpdateCandidate, UpdateChecker, version_key
from .manager import ResourceError, ResourceManager, ResourceState

if TYPE_CHECKING:  # pragma: no cover - 只为类型提示
    from ..config import AgentConfig

ATTEMPTS_NAME = "attempts.json"
FAILURE_COOLDOWN = 6 * 60 * 60  # 同一版本失败后 6 小时内不再自动重试

Prober = Callable[[Resource, Path], "tuple[bool, str]"]
ProgressCallback = Callable[[dict[str, Any]], None]

UPGRADED = "upgraded"
CURRENT = "current"
SKIPPED = "skipped"
FAILED = "failed"
ROLLED_BACK = "rolled_back"


@dataclass
class UpgradeResult:
    resource_id: str
    name: str
    from_version: str
    to_version: str
    status: str
    detail: str = ""
    probe: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in {UPGRADED, CURRENT, ROLLED_BACK}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.resource_id,
            "name": self.name,
            "from": self.from_version,
            "to": self.to_version,
            "status": self.status,
            "detail": self.detail,
            "probe": self.probe,
            "elapsed": round(self.elapsed, 2),
            "ok": self.ok,
        }


def reason_of(text: str, limit: int = 300) -> str:
    """从构建日志里挑出真正的原因，别把 pip 的进度条塞进面板。

    pip 的关键行是 `ERROR: Package 'x' requires a different Python: ...`，
    npm 的是 `npm ERR!`，所以从尾部往上找带错误特征的行。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith(("error", "npm err", "fatal")) or "error:" in lowered:
            return line[:limit]
    return (lines[-1] if lines else str(text or ""))[:limit]


def relocate(text: str, link: str, dest: str) -> str:
    """把配置里指向稳定入口的路径改到旁路目录。

    `vendor/Windows-MCP/.venv/Scripts/python.exe` 与 `vendor\\Windows-MCP`
    两种写法都要认；`cwd` 同样走这个函数。
    """
    if not text or not link or dest in text:
        return text
    for source, target in (
        (link, dest),
        (link.replace("/", "\\"), dest.replace("/", "\\")),
    ):
        if source and source in text:
            return text.replace(source, target)
    return text


def _dest_text(manager: ResourceManager, dest: Path) -> str:
    """给命令改写用的目标路径：能写成相对项目根的位置就写相对的，日志更好读。"""
    try:
        return dest.resolve().relative_to(manager.root.resolve()).as_posix()
    except (ValueError, OSError):
        return str(dest)


def make_prober(
    config: "AgentConfig", manager: ResourceManager
) -> Prober:
    """按资源类型给探针：内核跑 `--version`，MCP 服务跑真实握手。"""

    def probe(resource: Resource, dest: Path) -> tuple[bool, str]:
        if resource.kind == "browser":
            return manager.probe_browser_path(dest, resource.entry)
        adapter = next(
            (
                item
                for item in config.adapters.values()
                if item.kind == resource.id
            ),
            None,
        )
        if adapter is None or not adapter.command:
            return False, "配置里没有该后端，无法验证新版本"
        from ..bootstrap import probe_mcp_command

        # 注意：替换目标必须来自入参 dest（旁路目录），不能用 resource.dest
        # （那是清单版本）。用错就会「验证旧版、切换新版」——最危险的一种错。
        replacement = _dest_text(manager, dest)
        command = [relocate(part, resource.link, replacement) for part in adapter.command]
        cwd = relocate(adapter.cwd or "", resource.link, replacement) or None
        result = probe_mcp_command(
            command, cwd=cwd, env=adapter.env or None, timeout=30
        )
        tools = list(result.get("tools") or [])
        detail = str(result.get("detail") or "")
        if result.get("ok"):
            detail = f"{detail}（{', '.join(tools[:4])}{'…' if len(tools) > 4 else ''}）"
        return bool(result.get("ok")), detail

    return probe


class Upgrader:
    """把「发现新版本」变成「换上并验证过的新版本」，失败不留半成品。"""

    def __init__(
        self,
        manager: ResourceManager,
        checker: UpdateChecker | None = None,
        *,
        prober: Prober | None = None,
        cooldown: int = FAILURE_COOLDOWN,
    ) -> None:
        self.manager = manager
        self.checker = checker or UpdateChecker()
        self._prober = prober
        self.cooldown = cooldown

    # ------------------------------------------------------------ candidates
    def candidates(self, *, refresh: bool = False) -> list[UpdateCandidate]:
        return self.checker.check(self.manager.resources, refresh=refresh)

    def candidate_for(
        self, resource_id: str, version: str = "", *, refresh: bool = False
    ) -> UpdateCandidate | None:
        resource = self.manager.resource(resource_id)
        if version:
            target = clean_version(version)
            compatible, reason = self.checker.compatible(resource, target)
            return UpdateCandidate(
                resource_id=resource.id,
                name=resource.name,
                current=resource.version,
                latest=target,
                compatible=compatible,
                reason=reason,
            )
        for item in self.candidates(refresh=refresh):
            if item.resource_id == resource_id:
                return item
        return None

    def pending(
        self, candidates: Sequence[UpdateCandidate] | None = None
    ) -> list[UpdateCandidate]:
        """可以自动升级的候选：同大版本、且没在冷却期里。"""
        found = (
            list(candidates)
            if candidates is not None
            else self.candidates()
        )
        return [
            item
            for item in found
            if item.compatible
            and not self.blocked(item.resource_id, item.latest)
            and self.is_newer(item.resource_id, item.latest)
        ]

    def blocked(self, resource_id: str, version: str) -> bool:
        record = self.attempts().get(resource_id) or {}
        if clean_version(str(record.get("version") or "")) != clean_version(version):
            return False
        if record.get("status") == UPGRADED:
            return False
        try:
            last = float(record.get("ts") or 0)
        except (TypeError, ValueError):
            return False
        return time.time() - last < self.cooldown

    def is_newer(self, resource_id: str, version: str) -> bool:
        current = self.manager.expected(self.manager.resource(resource_id))
        return version_key(version) > version_key(current.version)

    # --------------------------------------------------------------- attempts
    @property
    def attempts_path(self) -> Path:
        return self.manager.root / ".xm-agent" / "resources" / ATTEMPTS_NAME

    def attempts(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.attempts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _record_attempt(
        self, resource_id: str, version: str, status: str, detail: str
    ) -> None:
        records = self.attempts()
        records[resource_id] = {
            "version": clean_version(version),
            "status": status,
            "detail": detail[:300],
            "ts": time.time(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        path = self.attempts_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass

    # ---------------------------------------------------------------- upgrade
    def upgrade(
        self,
        resource_id: str,
        version: str = "",
        *,
        candidate: UpdateCandidate | None = None,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> UpgradeResult:
        started = time.monotonic()
        resource = self.manager.resource(resource_id)
        current = self.manager.expected(resource)

        def result(
            status: str,
            *,
            to_version: str = "",
            detail: str = "",
            probe: str = "",
        ) -> UpgradeResult:
            return UpgradeResult(
                resource_id=resource.id,
                name=resource.name,
                from_version=current.version,
                to_version=to_version or clean_version(version),
                status=status,
                detail=detail,
                probe=probe,
                elapsed=time.monotonic() - started,
            )

        candidate = candidate or self.candidate_for(resource_id, version)
        if candidate is None:
            return result(CURRENT, detail="上游没有更新的版本")

        target = clean_version(version or candidate.latest)
        if not target:
            return result(CURRENT, detail="无法确定目标版本")
        if version_key(target) <= version_key(current.version):
            return result(CURRENT, to_version=target, detail="已是最新版本")
        if not (candidate.compatible or force):
            return result(
                SKIPPED,
                to_version=target,
                detail=candidate.reason or "兼容门禁未通过，需要人工确认",
            )

        def emit(
            phase: str, message: str, resource_percent: int | None = None
        ) -> None:
            if progress is None:
                return
            try:
                progress(
                    {
                        "event": "upgrade",
                        "resource": resource.id,
                        "phase": phase,
                        "status": "installing",
                        "message": message,
                        "resource_percent": resource_percent,
                    }
                )
            except Exception:
                pass

        emit("download", f"准备安装 {resource.name} {target}", 1)
        try:
            state = self.manager.stage(
                resource.id,
                target,
                ref=candidate.latest,
                progress=progress,
            )
            if not state.ok:
                raise ResourceError(state.detail or state.status)
            emit("probe", f"验证 {resource.name} {target} 能否真正启动", 92)
            ok, detail = self._probe(resource, Path(state.location))
            if not ok:
                raise ResourceError(f"探针未通过：{detail}")
            emit("switch", "探针通过，切换稳定入口", 96)
            switched: ResourceState = self.manager.switch(
                resource.id, target, ref=candidate.latest, probe=detail
            )
        except Exception as exc:
            reason = reason_of(str(exc))
            # 先记失败，再做清理：清理可能因为文件占用失败，失败记录不能丢
            self._record_attempt(resource.id, target, FAILED, reason)
            try:
                discarded = self.manager.discard(resource.id, target)
            except Exception:  # noqa: BLE001 - 清理失败不影响回滚结论
                discarded = False
            cleanup = "已删除旁路版本" if discarded else "旁路目录保留，可手动清理"
            emit("failed", f"升级失败，保留旧版：{reason}", 100)
            return result(
                FAILED,
                to_version=target,
                detail=f"{reason}（{cleanup}）",
            )

        removed = self.manager.prune(resource.id)
        self._record_attempt(resource.id, target, UPGRADED, detail)
        emit("done", f"已升级到 {target}", 100)
        keep = f"；清理旧版本 {', '.join(removed)}" if removed else ""
        return result(
            UPGRADED,
            to_version=switched.version,
            detail=f"从 {current.version} 升级到 {switched.version}{keep}",
            probe=detail,
        )

    def rollback(self, resource_id: str) -> UpgradeResult:
        started = time.monotonic()
        resource = self.manager.resource(resource_id)
        current = self.manager.expected(resource)
        ok, detail = self.manager.rollback(resource_id)
        return UpgradeResult(
            resource_id=resource.id,
            name=resource.name,
            from_version=current.version,
            to_version=resource.version,
            status=ROLLED_BACK if ok else FAILED,
            detail=detail,
            elapsed=time.monotonic() - started,
        )

    def _probe(self, resource: Resource, dest: Path) -> tuple[bool, str]:
        if self._prober is not None:
            return self._prober(resource, dest)
        if resource.kind == "browser":
            return self.manager.probe_browser_path(dest, resource.entry)
        return False, "没有为该资源配置探针，不能切换未验证的版本"


__all__ = [
    "CURRENT",
    "FAILED",
    "ROLLED_BACK",
    "SKIPPED",
    "UPGRADED",
    "UpgradeResult",
    "Upgrader",
    "make_prober",
    "reason_of",
    "relocate",
]
