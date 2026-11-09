from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from .manifest import (
    GITHUB_ARCHIVE_MIRRORS,
    NPM_REGISTRIES,
    PYPI_INDEXES,
    RESOURCES,
    Resource,
)

MARKER_NAME = ".xm-agent-resource.json"
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
                self._install(resource, state, phase, emit)
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

    # ----------------------------------------------------------------- inspect
    def _select(self, only: Sequence[str] | None) -> list[Resource]:
        if not only:
            return list(self.resources)
        wanted = {str(item) for item in only}
        known = {resource.id for resource in self.resources}
        unknown = wanted - known
        if unknown:
            raise ValueError(f"unknown resource: {', '.join(sorted(unknown))}")
        return [resource for resource in self.resources if resource.id in wanted]

    def _inspect(self, resource: Resource) -> ResourceState:
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

        self._ensure_link(resource, link, dest, phase)
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
            shutil.rmtree(dest)
            return
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
        text = f"{result.stderr or ''}\n{result.stdout or ''}".strip()
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

    def _read_version(self, resource: Resource, path: Path) -> str:
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
