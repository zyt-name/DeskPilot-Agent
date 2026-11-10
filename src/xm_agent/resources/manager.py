from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from .manifest import (
    GITHUB_ARCHIVE_MIRRORS,
    NPM_REGISTRIES,
    PYPI_INDEXES,
    RESOURCES,
    Resource,
    clean_version,
)
from .updates import version_key

MARKER_NAME = ".xm-agent-resource.json"
PINS_NAME = "active.json"
MAX_ARCHIVE_ENTRIES = 20000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
CHUNK = 64 * 1024

ProgressCallback = Callable[[dict[str, Any]], None]

READY = "ready"
MISSING = "missing"
INCOMPLETE = "incomplete"
OUTDATED = "outdated"
ERROR = "error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ResourceState:
    id: str
    name: str
    version: str
    required: bool
    purpose: str
    status: str
    detail: str = ""
    location: str = ""
    source: str = ""
    stage: str = ""
    license: str = ""
    # 清单里出厂钉住的版本；`version` 是实际在用的（可能被升级过）
    builtin_version: str = ""
    runtime_ok: bool | None = None
    runtime_detail: str = ""
    runtime_tools: list[str] = field(default_factory=list)
    runtime_checked_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == READY

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


class ResourceError(RuntimeError):
    pass


class ResourceManager:
    """Download, version and build the third-party MCP servers we depend on."""

    def __init__(
        self,
        root: str | Path = ".",
        *,
        resources: Sequence[Resource] | None = None,
        mirrors: Sequence[tuple[str, str]] | None = None,
        pypi_indexes: Sequence[tuple[str, str]] | None = None,
        npm_registries: Sequence[tuple[str, str]] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.resources = tuple(resources if resources is not None else RESOURCES)
        self.mirrors = tuple(
            mirrors if mirrors is not None else GITHUB_ARCHIVE_MIRRORS
        )
        self.pypi_indexes = tuple(
            pypi_indexes if pypi_indexes is not None else PYPI_INDEXES
        )
        self.npm_registries = tuple(
            npm_registries if npm_registries is not None else NPM_REGISTRIES
        )
        self.runner = runner or self._default_runner
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ status
    def status(self) -> list[ResourceState]:
        return [self._inspect(resource) for resource in self.resources]

    def ensure(
        self,
        only: Sequence[str] | None = None,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> list[ResourceState]:
        selected = self._select(only)
        total = len(selected)
        results: list[ResourceState] = []

        def emit(event: str, **payload: Any) -> None:
            if progress is None:
                return
            try:
                progress({"event": event, **payload})
            except Exception:
                pass

        for index, resource in enumerate(selected):
            detect_percent = 2 + int((index + 1) / total * 6)
            state = self._inspect(resource)
            emit(
                "detect",
                resource=resource.id,
                status=state.status,
                message=self._status_message(state),
                percent=detect_percent,
                resource_percent=100,
                state=state.to_dict(),
            )
            if state.status == READY and not force:
                results.append(state)
                continue

            base = 8 + index / total * 92
            span = 92 / total

            def phase(
                phase_name: str,
                message: str,
                resource_percent: float,
                _base: float = base,
                _span: float = span,
                _id: str = resource.id,
            ) -> None:
                percent = int(_base + _span * max(0.0, min(100.0, resource_percent)) / 100)
                emit(
                    "phase",
                    resource=_id,
                    phase=phase_name,
                    status="installing",
                    message=message,
                    percent=percent,
                    resource_percent=int(resource_percent),
                )

            try:
                target = self.expected(resource)
                self._install(target, state, phase, emit)
                state = self._inspect(resource)
            except Exception as exc:
                state = ResourceState(
                    id=resource.id,
                    name=resource.name,
                    version=resource.version,
                    required=resource.required,
                    purpose=resource.purpose,
                    status=ERROR,
                    detail=str(exc),
                    location=str(self.root / resource.link),
                    license=resource.license,
                    builtin_version=resource.version,
                )
                emit(
                    "error",
                    resource=resource.id,
                    status=ERROR,
                    message=str(exc),
                    percent=int(base + span),
                    resource_percent=100,
                    state=state.to_dict(),
                )
            results.append(state)
            emit(
                "result",
                resource=resource.id,
                status=state.status,
                message=self._status_message(state),
                percent=int(base + span),
                resource_percent=100,
                state=state.to_dict(),
            )
        return results

    def missing(self, only: Sequence[str] | None = None) -> list[ResourceState]:
        return [state for state in self.status() if not state.ok]

    # ------------------------------------------------------------------- pins
    def resource(self, resource_id: str) -> Resource:
        for item in self.resources:
            if item.id == resource_id:
                return item
        raise ValueError(f"unknown resource: {resource_id}")

    @property
    def pins_path(self) -> Path:
        return self.root / ".xm-agent" / "resources" / PINS_NAME

    def pins(self) -> dict[str, dict[str, Any]]:
        """已激活的升级版本：`{id: {version, ref, activated_at, probe}}`。

        清单里的版本是「出厂版本」，用户升级后实际在用的是这个记录。
        放在数据目录而不是源码里，升级不需要改代码。
        """
        try:
            data = json.loads(self.pins_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _write_pins(self, pins: dict[str, dict[str, Any]]) -> None:
        path = self.pins_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(pins, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def expected(self, resource: Resource) -> Resource:
        """实际应使用的版本：升级记录优先，回落到清单里钉住的版本。

        目录已经不在时不信记录——否则面板会一直报「缺失」而没法自愈。
        """
        pin = self.pins().get(resource.id) or {}
        version = clean_version(str(pin.get("version") or ""))
        if not version or version_key(version) <= version_key(resource.version):
            return resource
        dest = self.root / resource.install_dir / f"{resource.directory}-{version}"
        if not (dest / resource.entry).exists():
            return resource
        return replace(
            resource, version=version, ref=str(pin.get("ref") or version)
        )

    # ----------------------------------------------------------------- inspect
    def _select(self, only: Sequence[str] | None) -> list[Resource]:
        if not only:
            # 浏览器内核有 200MB+，绝不能在「检测资源完整性」时顺手拉下来。
            # 它是兜底引擎，只在用户显式安装、或引擎解析真的需要时才装。
            return [
                resource
                for resource in self.resources
                if resource.kind != "browser"
            ]
        wanted = {str(item) for item in only}
        known = {resource.id for resource in self.resources}
        unknown = wanted - known
        if unknown:
            raise ValueError(f"unknown resource: {', '.join(sorted(unknown))}")
        return [resource for resource in self.resources if resource.id in wanted]

    def _inspect(self, resource: Resource) -> ResourceState:
        expected = self.expected(resource)
        state = self._inspect_exact(expected)
        if (
            state.ok
            and expected.version != resource.version
            and resource.kind != "browser"
        ):
            # 记录说在用新版本，入口却指着别的目录：这种分裂必须报出来，
            # 否则面板显示「已就绪」，实际跑的可能是旧版。
            target = self._link_target(self.root / resource.link)
            dest = self.root / expected.dest
            try:
                mismatch = target is not None and target != dest.resolve()
            except OSError:
                mismatch = True
            if mismatch:
                name = target.name if target is not None else "（无入口）"
                return self._state(
                    expected,
                    OUTDATED,
                    f"入口指向 {name}，记录为 {expected.version}，可重新安装或回滚",
                    target or self.root / resource.link,
                )
        return state

    def _inspect_exact(self, resource: Resource) -> ResourceState:
        dest = self.root / resource.dest
        link = self.root / resource.link
        candidates: list[tuple[str, Path]] = []
        if dest.is_dir():
            candidates.append(("versioned", dest))
        if link.is_dir() and not (
            dest.is_dir() and link.resolve() == dest.resolve()
        ):
            candidates.append(("legacy", link))

        records: list[dict[str, Any]] = []
        for layout, path in candidates:
            marker = self._read_marker(path)
            version = str(marker.get("version") or self._read_version(resource, path))
            missing = [
                item
                for item in (resource.entry, *resource.runtime_checks)
                if not (path / item).exists()
            ]
            records.append(
                {
                    "layout": layout,
                    "path": path,
                    "version": version,
                    "missing": missing,
                    "source": str(marker.get("source") or ""),
                    "stage": str(marker.get("stage") or ""),
                }
            )

        for record in records:
            if (
                record["version"] == resource.version
                and not record["missing"]
                and record["stage"] in {"", "ready"}
            ):
                layout_label = (
                    "版本化目录" if record["layout"] == "versioned" else "本地已有安装"
                )
                return self._state(
                    resource,
                    READY,
                    f"已就绪（{layout_label}）",
                    record["path"],
                    record["source"],
                    record["stage"],
                )

        matching_version = [
            record for record in records if record["version"] == resource.version
        ]
        if matching_version:
            record = matching_version[0]
            if record["stage"] and record["stage"] != "ready":
                detail = (
                    f"上次安装未完成（阶段：{record['stage']}），需要重新安装"
                )
            else:
                detail = (
                    "版本匹配但缺少运行文件："
                    + ", ".join(record["missing"])
                )
            return self._state(
                resource,
                INCOMPLETE,
                detail,
                record["path"],
                record["source"],
                record["stage"],
            )

        versioned = [record for record in records if record["layout"] == "versioned"]
        local_versions = [
            record["version"] for record in records if record["version"]
        ]
        if local_versions:
            record = (versioned or records)[0]
            return self._state(
                resource,
                OUTDATED,
                f"本地版本 {', '.join(sorted(set(local_versions)))}，需要 {resource.version}",
                record["path"],
                record["source"],
                record["stage"],
            )

        if records:
            record = records[0]
            return self._state(
                resource,
                INCOMPLETE,
                "目录存在但无法识别版本，需要重新安装",
                record["path"],
                record["source"],
                record["stage"],
            )

        return self._state(resource, MISSING, "未安装")

    def _state(
        self,
        resource: Resource,
        status: str,
        detail: str,
        path: Path | None = None,
        source: str = "",
        stage: str = "",
    ) -> ResourceState:
        builtin = next(
            (item.version for item in self.resources if item.id == resource.id),
            resource.version,
        )
        return ResourceState(
            id=resource.id,
            name=resource.name,
            version=resource.version,
            required=resource.required,
            purpose=resource.purpose,
            status=status,
            detail=detail,
            location=str(path or (self.root / resource.link)),
            source=source,
            stage=stage,
            license=resource.license,
            builtin_version=builtin,
        )

    @staticmethod
    def _status_message(state: ResourceState) -> str:
        labels = {
            READY: "已就绪",
            MISSING: "未安装",
            INCOMPLETE: "需要补全",
            OUTDATED: "版本不匹配",
            ERROR: "安装失败",
        }
        detail = f"：{state.detail}" if state.detail and state.status != READY else ""
        return f"{state.name} {labels.get(state.status, state.status)}{detail}"

    # ----------------------------------------------------------------- install
    def _install(
        self,
        resource: Resource,
        state: ResourceState,
        phase: Callable[[str, str, float], None],
        emit: Callable[..., None],
        *,
        switch_link: bool = True,
    ) -> None:
        dest = self.root / resource.dest
        link = self.root / resource.link
        dest.parent.mkdir(parents=True, exist_ok=True)

        marker = self._read_marker(dest) if dest.is_dir() else {}
        source = str(marker.get("source") or "")
        if not (dest.is_dir() and marker.get("version") == resource.version):
            if dest.exists() or dest.is_symlink() or self._is_junction(dest):
                self._retire(dest, resource)
            phase("download", f"下载 {resource.name} {resource.version}", 2)
            archive, source = self._download(resource, phase)
            phase("extract", "校验并解压归档", 72)
            self._extract_archive(archive, dest)
            self._write_marker(
                resource, dest, stage="source", source=source
            )
        else:
            source = source or "cache"
            emit(
                "phase",
                resource=resource.id,
                phase="download",
                status="installing",
                message="源码已缓存，跳过下载",
                percent=None,
                resource_percent=74,
            )

        if resource.kind == "browser":
            # 内核是解压即用的二进制，不需要构建，也不需要稳定入口链接：
            # 引擎解析按 chromium-<version>/chrome.exe 直接找最新版本。
            phase("verify", "校验内核文件", 90)
        else:
            if switch_link:
                self._ensure_link(resource, link, dest, phase)
            else:
                # 旁路安装：稳定入口仍然指向旧版本，探针通过前不切
                phase("link", "旁路安装，暂不切换入口", 80)
            phase("build", "准备运行环境", 82)
            if resource.kind == "python":
                self._build_python(resource, dest, phase)
            elif resource.kind == "node":
                self._build_node(resource, dest, phase)
            else:
                raise ResourceError(f"unknown resource kind: {resource.kind}")

        if not (dest / resource.entry).exists():
            raise ResourceError(f"构建完成但缺少入口文件：{resource.entry}")
        self._write_marker(resource, dest, stage="ready", source=source)
        phase("done", "资源已就绪", 100)

    def _retire(self, dest: Path, resource: Resource) -> None:
        if self._is_link(dest):
            self._remove_link(dest)
            return
        marker = self._read_marker(dest)
        if marker:
            try:
                shutil.rmtree(dest)
                return
            except OSError:
                # 删不掉就搬去备份区：残留目录不该让整次安装失败
                pass
        backup_root = self.root / ".xm-agent" / "resource-backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = backup_root / f"{resource.directory}-{stamp}"
        suffix = 1
        while target.exists():
            suffix += 1
            target = backup_root / f"{resource.directory}-{stamp}-{suffix}"
        shutil.move(str(dest), str(target))

    def _download(
        self,
        resource: Resource,
        phase: Callable[[str, str, float], None],
    ) -> tuple[Path, str]:
        cache_dir = self.root / ".xm-agent" / "resources"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / f"{resource.id}-{resource.version}.zip"
        if cache.is_file() and zipfile.is_zipfile(cache):
            return cache, "本地缓存"

        if resource.sources:
            # 显式下载源（如 Chrome for Testing）自带官方与国内镜像，
            # 不再套 GitHub 代理——代理对非 GitHub 域名无效。
            sources = [(label, url, 60) for label, url in resource.sources]
        else:
            sources = [("GitHub", resource.archive, 20)]
            sources += [
                (name, template.format(url=resource.archive), 60)
                for name, template in self.mirrors
            ]
        errors: list[str] = []
        part = cache.with_suffix(".zip.part")
        for label, url, timeout in sources:
            phase("download", f"尝试下载源：{label}", 4)
            try:
                self._fetch(url, part, timeout, phase)
                if not zipfile.is_zipfile(part):
                    raise ResourceError("下载内容不是有效的 zip 归档")
                os.replace(part, cache)
                return cache, label
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                part.unlink(missing_ok=True)
        raise ResourceError(
            f"{resource.name} 下载失败，所有源均不可用：" + "；".join(errors)
        )

    def _fetch(
        self,
        url: str,
        target: Path,
        timeout: int,
        phase: Callable[[str, str, float], None],
    ) -> None:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "xm-agent/0.1",
                "Accept": "application/zip, application/octet-stream",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise ResourceError(f"HTTP {status}")
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(target, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        fraction = min(66.0, downloaded / total * 66)
                    else:
                        fraction = min(60.0, downloaded / (2 * 1024 * 1024) * 60)
                    phase(
                        "download",
                        f"下载中 {downloaded / 1048576:.1f} MB",
                        fraction,
                    )

    def _extract_archive(self, archive: Path, dest: Path) -> None:
        tmp_root = self.root / ".xm-agent" / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="xm-resource-", dir=tmp_root))
        try:
            self.safe_extract(archive, work)
            entries = [item for item in work.iterdir()]
            if len(entries) != 1 or not entries[0].is_dir():
                raise ResourceError("压缩包结构异常：需要单一顶层目录")
            if dest.exists():
                raise ResourceError(f"目标目录已存在：{dest}")
            shutil.move(str(entries[0]), str(dest))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def safe_extract(self, archive: Path, target: Path) -> None:
        target = target.resolve()
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ResourceError("压缩包条目过多，已拒绝解压")
            if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                raise ResourceError("压缩包解压后体积过大，已拒绝解压")
            for info in infos:
                name = info.filename.replace("\\", "/")
                if "\x00" in name:
                    raise ResourceError("压缩包包含非法路径")
                parts = PurePosixPath(name).parts
                if not parts:
                    continue
                if (
                    name.startswith("/")
                    or PurePosixPath(name).is_absolute()
                    or ":" in parts[0]
                    or any(part == ".." for part in parts)
                ):
                    raise ResourceError(f"压缩包包含越界路径：{info.filename}")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    continue
                destination = (target / PurePosixPath(name)).resolve()
                if destination != target and target not in destination.parents:
                    raise ResourceError(f"压缩包包含越界路径：{info.filename}")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, open(destination, "wb") as sink:
                    shutil.copyfileobj(source, sink, CHUNK)

    # ------------------------------------------------------------------- build
    def _build_python(
        self,
        resource: Resource,
        dest: Path,
        phase: Callable[[str, str, float], None],
    ) -> None:
        venv = dest / ".venv"
        if os.name == "nt":
            python = venv / "Scripts" / "python.exe"
        else:
            python = venv / "bin" / "python"
        if not python.exists():
            phase("build", "创建 Python 虚拟环境", 84)
            result = self._run(
                [sys.executable, "-m", "venv", str(venv)], dest, {}, 300
            )
            if result.returncode != 0:
                raise ResourceError(
                    "创建虚拟环境失败：" + self._tail(result)
                )

        attempts: list[tuple[str, str | None]] = [("默认源", None)]
        attempts += [
            (name, index)
            for name, index in self.pypi_indexes
            if name != "pypi"
        ]
        last: subprocess.CompletedProcess | None = None
        for name, index in attempts:
            phase("build", f"安装 Python 依赖（{name}）", 88)
            command = [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--retries",
                "2",
                "--timeout",
                "30",
                "-e",
                ".",
            ]
            if index:
                command += ["--index-url", index]
            last = self._run(
                command,
                dest,
                {
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PIP_NO_INPUT": "1",
                },
                900,
            )
            if last.returncode == 0:
                return
        raise ResourceError(
            "Python 依赖安装失败（已尝试 PyPI 与国内镜像）："
            + (self._tail(last) if last else "")
        )

    def _build_node(
        self,
        resource: Resource,
        dest: Path,
        phase: Callable[[str, str, float], None],
    ) -> None:
        attempts: list[tuple[str, str | None]] = [("默认 npm", None)]
        attempts += [
            (name, registry)
            for name, registry in self.npm_registries
            if name != "npmjs"
        ]
        last: subprocess.CompletedProcess | None = None
        last_env: dict[str, str] = {}
        for name, registry in attempts:
            phase("build", f"安装 Node 依赖（{name}）", 88)
            env = {
                "npm_config_fetch_retries": "2",
                "npm_config_audit": "false",
                "npm_config_fund": "false",
            }
            if registry:
                env["npm_config_registry"] = registry
            last = self._run(
                self._npm_command("install", "--no-audit", "--no-fund"),
                dest,
                env,
                900,
            )
            last_env = env
            if last.returncode == 0:
                break
        else:
            raise ResourceError(
                "Node 依赖安装失败（已尝试 npm 官方与 npmmirror）："
                + (self._tail(last) if last else "")
            )

        phase("build", "编译 TypeScript", 94)
        result = self._run(self._npm_command("run", "build"), dest, last_env, 600)
        if result.returncode != 0:
            raise ResourceError("Node 构建失败：" + self._tail(result))

    def _npm_command(self, *args: str) -> list[str]:
        npm = shutil.which("npm") or ("npm.cmd" if os.name == "nt" else "npm")
        if os.name == "nt":
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            return [
                comspec,
                "/d",
                "/c",
                subprocess.list2cmdline([npm, *args]),
            ]
        return [npm, *args]

    @staticmethod
    def _default_runner(
        command: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        full_env.update(env)
        return subprocess.run(
            list(command),
            cwd=str(cwd),
            env=full_env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )

    def _run(
        self,
        command: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess:
        try:
            return self.runner(command, cwd, env, timeout)
        except FileNotFoundError as exc:
            raise ResourceError(f"缺少命令：{command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ResourceError(f"命令超时（{timeout}s）：{command[0]}") from exc

    @staticmethod
    def _tail(result: subprocess.CompletedProcess | None) -> str:
        if result is None:
            return ""
        # stderr 放后面：pip / npm 的致命错误都写在 stderr，取尾部才不会把它截掉
        text = "\n".join(
            part
            for part in (result.stdout or "", result.stderr or "")
            if part
        )
        text = text.replace("\r", "")
        return text[-600:]

    # -------------------------------------------------------------------- link
    def _ensure_link(
        self,
        resource: Resource,
        link: Path,
        dest: Path,
        phase: Callable[[str, str, float], None],
    ) -> None:
        if link.exists() or self._is_link(link):
            if link.resolve() == dest.resolve():
                phase("link", "稳定入口已指向当前版本", 80)
                return
            if self._is_link(link):
                self._remove_link(link)
            else:
                self._retire(link, resource)
                phase(
                    "link",
                    "旧目录已备份，切换到版本化目录",
                    80,
                )
        phase("link", "创建稳定入口 vendor/" + resource.directory, 80)
        self._create_link(link, dest)

    def _create_link(self, link: Path, dest: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            result = subprocess.run(
                [comspec, "/d", "/c", "mklink", "/J", str(link), str(dest)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            )
            if result.returncode != 0:
                raise ResourceError(
                    "创建版本链接失败：" + (result.stderr or result.stdout or "").strip()
                )
            return
        os.symlink(dest, link, target_is_directory=True)

    @staticmethod
    def _is_junction(path: Path) -> bool:
        check = getattr(path, "is_junction", None)
        if check is None:
            return False
        try:
            return bool(check())
        except OSError:
            return False

    @classmethod
    def _is_link(cls, path: Path) -> bool:
        return path.is_symlink() or cls._is_junction(path)

    @staticmethod
    def _remove_link(path: Path) -> None:
        try:
            os.rmdir(path)
        except OSError:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ marker
    def _read_marker(self, path: Path) -> dict[str, Any]:
        marker = path / MARKER_NAME
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_marker(
        self, resource: Resource, dest: Path, *, stage: str, source: str
    ) -> None:
        payload = {
            **resource.to_dict(),
            "stage": stage,
            "source": source,
            "installed_at": _now(),
        }
        (dest / MARKER_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def probe_browser(self, resource_id: str = "chromium") -> tuple[bool, str]:
        """启动探针：真的跑一次 `--version`，确认内核能起来。

        只看文件存在不够——解压不全的 chrome.exe 也会存在，但一启动就退出。
        """
        resource = next(
            (item for item in self.resources if item.id == resource_id), None
        )
        if resource is None or resource.kind != "browser":
            return False, "不是浏览器内核资源"
        state = self._inspect(resource)
        if not state.ok:
            return False, state.detail or state.status
        return self.probe_browser_path(Path(state.location))

    def probe_browser_path(self, path: Path, entry: str = "") -> tuple[bool, str]:
        """对指定目录跑内核启动探针，用于旁路安装的候选版本。"""
        executable = Path(path) / (entry or "chrome.exe")
        if not executable.exists():
            return False, f"缺少内核文件：{executable.name}"
        try:
            result = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"内核启动失败：{exc}"
        text = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0 or not text:
            return False, f"内核未正常响应（退出码 {result.returncode}）"
        return True, text.splitlines()[0][:120]

    # ----------------------------------------------------------------- upgrade
    def stage(
        self,
        resource_id: str,
        version: str,
        *,
        ref: str = "",
        progress: ProgressCallback | None = None,
    ) -> ResourceState:
        """把新版本装到旁路目录，**不动**稳定入口。

        升级的第一道关：装完、构建完都只是候选，能不能用由探针说了算，
        探针之前任何东西都不许指向它。
        """
        resource = self.resource(resource_id)
        target = resource.with_version(version, ref)

        def emit(event: str, **payload: Any) -> None:
            if progress is None:
                return
            try:
                progress({"event": event, **payload})
            except Exception:
                pass

        def phase(name: str, message: str, resource_percent: float) -> None:
            emit(
                "phase",
                resource=target.id,
                phase=name,
                status="installing",
                message=message,
                resource_percent=max(0, min(100, int(resource_percent))),
            )

        with self._lock:
            state = self._inspect_exact(target)
            if state.status != READY:
                self._install(
                    target,
                    state,
                    phase,
                    emit,
                    switch_link=False,
                )
                state = self._inspect_exact(target)
        return state

    def switch(
        self,
        resource_id: str,
        version: str,
        *,
        ref: str = "",
        probe: str = "",
    ) -> ResourceState:
        """探针通过后才切换稳定入口，并记下在用版本。"""
        resource = self.resource(resource_id)
        target = resource.with_version(version, ref)
        dest = self.root / target.dest
        if not (dest / target.entry).exists():
            raise ResourceError(f"{target.name} {target.version} 尚未安装完成")

        link = self.root / resource.link
        previous = self._link_target(link)
        current = self.expected(resource)
        if resource.kind == "browser":
            # 内核没有稳定入口（引擎解析按目录找），在用版本由启用记录决定
            with self._lock:
                self._pin(resource, target, current.version, probe)
            return self._inspect(resource)
        with self._lock:
            try:
                self._clear_link(link, resource)
                self._create_link(link, dest)
            except Exception:
                # 切一半失败就等于入口没了，必须立刻把旧链接接回去
                self._relink(link, previous, resource)
                raise
            self._pin(resource, target, current.version, probe)

        state = self._inspect(resource)
        if not state.ok:
            pins = self.pins()
            pins.pop(resource.id, None)
            self._write_pins(pins)
            self._relink(link, previous, resource)
            raise ResourceError(f"切换后状态异常：{state.detail or state.status}")
        return state

    def _pin(
        self,
        resource: Resource,
        target: Resource,
        previous_version: str,
        probe: str = "",
    ) -> None:
        pins = self.pins()
        pins[resource.id] = {
            "version": target.version,
            "ref": target.ref,
            "from": previous_version,
            "probe": probe[:200],
            "activated_at": _now(),
        }
        self._write_pins(pins)

    def discard(self, resource_id: str, version: str) -> bool:
        """删掉旁路装的候选版本；正在用的那个不删。"""
        resource = self.resource(resource_id)
        target = resource.with_version(version)
        dest = self.root / target.dest
        if not dest.is_dir() or self._is_link(dest):
            return False
        if self._link_target(self.root / resource.link) == dest.resolve():
            return False
        shutil.rmtree(dest, ignore_errors=True)
        return not dest.exists()

    def prune(self, resource_id: str, keep: int = 1) -> list[str]:
        """回收多余版本目录，永远保留在用版本与清单版本。"""
        resource = self.resource(resource_id)
        protected = {self.expected(resource).version, resource.version}
        root = self.root / resource.install_dir
        if not root.is_dir():
            return []
        prefix = f"{resource.directory}-"
        candidates: list[tuple[tuple[int, ...], str, Path]] = []
        for path in root.glob(f"{prefix}*"):
            if not path.is_dir() or self._is_link(path):
                continue
            version = clean_version(path.name[len(prefix) :])
            if not version or version in protected:
                continue
            if not self._read_marker(path):
                continue
            candidates.append((version_key(version), version, path))
        candidates.sort(reverse=True)
        removed: list[str] = []
        for _, version, path in candidates[keep:]:
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed.append(version)
        return removed

    def rollback(self, resource_id: str) -> tuple[bool, str]:
        """取消版本锁定，把在用版本切回清单里的出厂版本。"""
        resource = self.resource(resource_id)
        pins = self.pins()
        previous_pin = pins.pop(resource_id, None)
        if previous_pin is None:
            return True, f"{resource.name} 没有升级记录，当前即 {resource.version}"
        upgraded = clean_version(str(previous_pin.get("version") or ""))

        dest = self.root / resource.dest
        if not (dest / resource.entry).exists():
            self._write_pins(pins)
            return False, (
                f"已取消 {upgraded} 的锁定，"
                f"但出厂版本 {resource.version} 的目录不在，需要重新安装"
            )
        if resource.kind == "browser":
            # 内核没有链接可改，写一条指向出厂版本的启用记录即可
            self._pin(resource, resource, upgraded, "rollback")
            return True, f"已切回 {resource.version}（内置内核按启用版本解析）"

        self._write_pins(pins)
        link = self.root / resource.link
        previous = self._link_target(link)
        try:
            self._clear_link(link, resource)
            self._create_link(link, dest)
        except Exception as exc:
            self._relink(link, previous, resource)
            return False, f"切回 {resource.version} 失败：{exc}"
        return True, f"已切回 {resource.version}"
    def _link_target(self, link: Path) -> Path | None:
        if not (link.exists() or self._is_link(link)):
            return None
        try:
            return link.resolve()
        except OSError:
            return None

    def _clear_link(self, link: Path, resource: Resource) -> None:
        if self._is_link(link):
            self._remove_link(link)
        elif link.exists():
            # 老布局是真实目录，搬去备份区，别直接删用户的东西
            self._retire(link, resource)

    def _relink(
        self, link: Path, previous: Path | None, resource: Resource
    ) -> None:
        if previous is None or not previous.is_dir():
            return
        try:
            self._clear_link(link, resource)
            self._create_link(link, previous)
        except Exception:
            pass

    def _read_version(self, resource: Resource, path: Path) -> str:
        if resource.kind == "browser":
            return str(self._read_marker(path).get("version") or "")
        if resource.kind == "python":
            for name in ("manifest.json", "package.json"):
                version = self._json_version(path / name)
                if version:
                    return version
            return ""
        if resource.kind == "node":
            return self._json_version(path / "package.json")
        return ""

    @staticmethod
    def _json_version(path: Path) -> str:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if isinstance(data, dict) and data.get("version"):
            return str(data["version"])
        return ""


__all__ = [
    "ERROR",
    "INCOMPLETE",
    "MISSING",
    "OUTDATED",
    "READY",
    "ResourceError",
    "ResourceManager",
    "ResourceState",
]
