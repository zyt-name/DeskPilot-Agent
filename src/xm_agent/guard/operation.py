"""Normalize a proposed action into an auditable operation descriptor.

The descriptor is built from the actual capability arguments, never from the
executing model's natural-language rationale. Rule detection here is
deterministic; the top-level auditor may only tighten its result.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable

from ..types import Action, Observation

READ_ONLY_CAPABILITIES = {
    "desktop.observe",
    "desktop.verify",
    "browser.observe",
    "browser.verify",
    "wait.for",
    "task.finish",
    "task.await_user",
}

READ_ONLY_DESKTOP_ACTIONS = {"scroll", "move"}

# 浏览器里「只看不动」的动作。滚动会改变视口，但不会改远端状态。
READ_ONLY_BROWSER_ACTIONS = {"scroll", "wheel", "wait", "wait_text"}

AUDIT_CONTROL_KEYS = {
    "audit_note",
    "sensitive",
    "sensitive_categories",
    "audit_effects",
}

_SECRET_KEYS = {
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "private_key",
    "credential",
    "cookie",
}

# Writes here are refused in every mode unless an advanced override is
# explicitly configured. Reads are still audited, not hard-blocked.
DEFAULT_HARD_BLOCK_PATHS = (
    "C:/Windows",
    "C:/Program Files",
    "C:/Program Files (x86)",
    "C:/ProgramData/Microsoft/Windows/Start Menu",
    "C:/System Volume Information",
    "C:/$Recycle.Bin",
    "C:/Boot",
    "C:/pagefile.sys",
    "C:/hiberfil.sys",
)

DEFAULT_USER_DATA_PATHS = (
    "%USERPROFILE%/Desktop",
    "%USERPROFILE%/Documents",
    "%USERPROFILE%/Downloads",
    "%USERPROFILE%/Pictures",
    "%USERPROFILE%/Videos",
    "%USERPROFILE%/Music",
    "%USERPROFILE%/OneDrive",
    "%USERPROFILE%/Favorites",
)

DEFAULT_PROTECTED_PATHS = (
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.config/gh",
    "%APPDATA%/Microsoft/Credentials",
    "%LOCALAPPDATA%/Microsoft/Credentials",
    "%LOCALAPPDATA%/Google/Chrome/User Data",
    "%LOCALAPPDATA%/Microsoft/Edge/User Data",
    "%APPDATA%/Mozilla/Firefox/Profiles",
    "*/id_rsa",
    "*/id_ed25519",
    "*.pem",
    "*.pfx",
    "*.p12",
    "*/.env",
)

_RECURSIVE_RE = re.compile(r"(?:^|\s)(?:-r|--recursive|-recurse|/s)(?:\s|$)", re.I)
_DELETE_RE = re.compile(
    r"\b(?:del|erase|rm|rmdir|rd|remove-item|clear-content)\b", re.I
)
_OVERWRITE_RE = re.compile(
    r"(?:^|[^>])>{1,2}\s*[^>\s]|\bcopy\b[^\n]*/y|\bmove\b[^\n]*/y|"
    r"\bset-content\b|\bout-file\b",
    re.I,
)
_EXECUTE_RE = re.compile(
    r"\b(?:curl|wget|invoke-webrequest|invoke-restmethod|irm|iwr)\b|"
    r"\|\s*(?:iex|invoke-expression|bash|sh|powershell)\b|"
    r"\.(?:exe|bat|cmd|ps1|vbs|js)\b",
    re.I,
)
_PRIVILEGE_RE = re.compile(
    r"\b(?:runas|sudo|su)\b|start-process[^\n]*-verb\s+runas|"
    r"\b(?:bcdedit|diskpart|format|mkfs|vssadmin)\b",
    re.I,
)
_SERVICE_RE = re.compile(
    r"\bsc(?:\.exe)?\s+(?:create|delete|config|stop|start)\b|"
    r"\bnet\s+(?:start|stop|user|localgroup)\b|"
    r"\bnew-service\b|\bset-service\b",
    re.I,
)
_REGISTRY_RE = re.compile(r"\breg(?:\.exe)?\s+(?:add|delete|copy|import)\b", re.I)
_STARTUP_RE = re.compile(
    r"\bschtasks\b|\bnew-scheduledtask\b|\bstartup\b|"
    r"currentversion\\run\b",
    re.I,
)
_PERMISSION_RE = re.compile(
    r"\b(?:icacls|takeown|chmod|chown|attrib)\b|\bset-acl\b", re.I
)
_SECURITY_RE = re.compile(
    r"\b(?:set-mppreference|netsh\s+advfirewall|defender|antivirus|"
    r"tamperprotection)\b",
    re.I,
)
_CREDENTIAL_RE = re.compile(
    r"\b(?:mimikatz|sekurlsa|vaultcmd|cmdkey|credential|sam\s+dump)\b", re.I
)
_NETWORK_RE = re.compile(
    r"\b(?:ssh|scp|sftp|ftp|curl|wget|nc|telnet|invoke-webrequest)\b", re.I
)
_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b", re.I)
_GIT_FORCE_PUSH_RE = re.compile(r"\bgit\s+push\b[^\n]*(?:--force|-f)\b", re.I)
_DROP_RE = re.compile(r"\bdrop\s+(?:database|table)\b", re.I)

_READ_ONLY_COMMAND_RE = re.compile(
    r"^\s*(?:dir|ls|pwd|cd|whoami|hostname|ver|type|cat|more|head|tail|"
    r"findstr|grep|where|which|tree|systeminfo|tasklist|"
    r"git\s+(?:status|log|diff|show|branch)|"
    r"python(?:\.exe)?\s+--version|node\s+--version|"
    r"echo(?:\s+[^>|]*)?)\b",
    re.I,
)

_WRITE_EFFECTS = {
    "delete",
    "recursive",
    "overwrite",
    "privilege",
    "service_change",
    "registry_change",
    "startup_change",
    "permission_change",
    "security_change",
    "system_change",
    "external_write",
    "git_push",
    "git_force_push",
    "credential_access",
}

_DESTRUCTIVE_UI_RE = re.compile(
    r"删除|移除|卸载|格式化|清空|清除|重置|移除|delete|remove|uninstall|"
    r"format|erase|wipe|clear|reset|empty recycle|shred|discard",
    re.I,
)
_DESTRUCTIVE_KEYS_RE = re.compile(
    r"(?:shift\+)?delete|shift\+del|alt\+f4|win\+l|ctrl\+shift\+esc",
    re.I,
)
_COMMIT_UI_RE = re.compile(
    r"发送|提交|发布|确认|购买|支付|付款|下单|删除|移除|卸载|格式化|清空|"
    r"重置|撤销|退订|注销|send|submit|publish|post|confirm|buy|pay|"
    r"purchase|order|delete|remove|uninstall|format|reset|unsubscribe",
    re.I,
)

_ROLLBACK_EFFECTS = {
    "delete",
    "recursive",
    "overwrite",
    "external_write",
    "upload",
    "git_push",
    "git_force_push",
    "privilege",
    "service_change",
    "registry_change",
    "startup_change",
    "permission_change",
    "security_change",
    "system_change",
    "credential_access",
    "ui_destructive",
    "ui_commit",
}

_PATH_TOKEN_RE = re.compile(
    r"""(?x)
    (?:
        [A-Za-z]:[\\/][^\s"'<>|]+
      | \\\\[^\s"'<>|]+
      | ~[\\/][^\s"'<>|]+
      | /(?:[\w.$-]+/)+[\w.$-]+
    )
    """
)


@dataclass
class OperationTarget:
    path: str
    exists: bool = False
    kind: str = "unknown"
    inside_workspace: bool = False
    inside_project: bool = False
    protected: bool = False
    user_data: bool = False
    hard_blocked: bool = False
    size_bytes: int | None = None
    git_repo: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "kind": self.kind,
            "inside_workspace": self.inside_workspace,
            "inside_project": self.inside_project,
            "protected": self.protected,
            "user_data": self.user_data,
            "hard_blocked": self.hard_blocked,
            "size_bytes": self.size_bytes,
            "git_repo": self.git_repo,
        }


@dataclass
class OperationDescriptor:
    operation_id: str
    capability: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    targets: list[OperationTarget] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    reversible: bool | None = None
    risk_hint: str = "low"
    audit_note: str = ""
    declared_sensitive: bool = False
    declared_categories: list[str] = field(default_factory=list)
    mutating: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "capability": self.capability,
            "action": self.action,
            "args": self.args,
            "targets": [target.to_dict() for target in self.targets],
            "effects": list(self.effects),
            "reversible": self.reversible,
            "risk_hint": self.risk_hint,
            "audit_note": self.audit_note,
            "declared_sensitive": self.declared_sensitive,
            "declared_categories": list(self.declared_categories),
            "mutating": self.mutating,
        }

    def summary(self) -> str:
        capability = self.capability
        action = self.action or capability
        detail = ""
        if capability == "terminal.exec":
            detail = str(self.args.get("command", ""))
        elif capability == "file.transfer":
            detail = (
                f"{self.args.get('direction', '?')} "
                f"{self.args.get('source', '?')} -> "
                f"{self.args.get('destination', '?')}"
            )
        elif capability in {"desktop.act", "browser.act"}:
            detail = str(
                self.args.get("text")
                or self.args.get("keys")
                or self.args.get("url")
                or self.args.get("element_index")
                or self.args.get("element_id")
                or self.args.get("x", "")
            )
        elif self.targets:
            detail = self.targets[0].path
        detail = detail.strip()
        if len(detail) > 160:
            detail = detail[:160] + "…"
        return f"{capability}:{action}" + (f" {detail}" if detail else "")


@dataclass(frozen=True)
class Sensitivity:
    sensitive: bool = False
    categories: tuple[str, ...] = ()
    risk: str = "low"
    reasons: tuple[str, ...] = ()
    hard_blocked: bool = False
    requires_user: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensitive": self.sensitive,
            "categories": list(self.categories),
            "risk": self.risk,
            "reasons": list(self.reasons),
            "hard_blocked": self.hard_blocked,
            "requires_user": self.requires_user,
        }


def clean_args(args: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in args.items()
        if str(key) not in AUDIT_CONTROL_KEYS
    }


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if str(key).lower() in _SECRET_KEYS
                else _redact(item, depth + 1)
            )
            for key, item in value.items()
            if str(key) not in AUDIT_CONTROL_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:50]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "...<truncated>"
    return value


def _expand(value: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    return expanded.strip().strip('"').strip("'")


def _normalize_path(value: str) -> Path | None:
    text = _expand(value)
    if not text:
        return None
    try:
        return Path(os.path.realpath(text))
    except (OSError, ValueError):
        try:
            return Path(text).absolute()
        except (OSError, ValueError):
            return None


def _normcase(path: str) -> str:
    return os.path.normcase(str(path)).replace("/", "\\")


def _within(path: Path, root: Path) -> bool:
    child = _normcase(str(path)).rstrip("\\")
    parent = _normcase(str(root)).rstrip("\\")
    return child == parent or child.startswith(parent + "\\")


def _pattern_match(path: Path, pattern: str) -> bool:
    normalized = _normcase(str(path))
    target = _normcase(_expand(pattern))
    if not target:
        return False
    if "*" in target or "?" in target:
        parts = normalized.split("\\")
        prefixes = ["\\".join(parts[: index + 1]) for index in range(len(parts))]
        return any(fnmatch(prefix, target) for prefix in prefixes)
    return normalized == target or normalized.startswith(target.rstrip("\\") + "\\")


def _any_pattern(path: Path, patterns: Iterable[str]) -> bool:
    return any(_pattern_match(path, pattern) for pattern in patterns if pattern)


def _target_info(
    path: Path,
    workspace_roots: list[Path],
    project_root: Path,
    protected_paths: list[str],
    hard_block_paths: list[str],
) -> OperationTarget:
    exists = False
    kind = "unknown"
    size: int | None = None
    try:
        exists = path.exists()
        if exists:
            if path.is_dir():
                kind = "dir"
            elif path.is_file():
                kind = "file"
            else:
                kind = "other"
            if path.is_file():
                size = max(0, int(path.stat().st_size))
    except OSError:
        pass
    git_repo = False
    probe = path
    for _ in range(8):
        try:
            if (probe / ".git").exists():
                git_repo = True
                break
        except OSError:
            break
        if probe.parent == probe:
            break
        probe = probe.parent
    inside_workspace = any(_within(path, root) for root in workspace_roots)
    inside_project = _within(path, project_root)
    return OperationTarget(
        path=str(path),
        exists=exists,
        kind=kind,
        inside_workspace=inside_workspace,
        inside_project=inside_project,
        protected=_any_pattern(path, protected_paths),
        user_data=(
            not inside_project
            and not inside_workspace
            and _any_pattern(path, DEFAULT_USER_DATA_PATHS)
        ),
        hard_blocked=_any_pattern(path, hard_block_paths),
        size_bytes=size,
        git_repo=git_repo,
    )


def _command_effects(command: str) -> tuple[list[str], bool | None]:
    effects: list[str] = []
    reversible: bool | None = None
    if _DELETE_RE.search(command):
        effects.append("delete")
        reversible = False
    if _RECURSIVE_RE.search(command):
        effects.append("recursive")
        reversible = False
    if _OVERWRITE_RE.search(command):
        effects.append("overwrite")
        reversible = False
    if _EXECUTE_RE.search(command):
        effects.append("execute")
    if _PRIVILEGE_RE.search(command):
        effects.append("privilege")
        effects.append("system_change")
        reversible = False
    if _SERVICE_RE.search(command):
        effects.append("service_change")
        effects.append("system_change")
        reversible = False
    if _REGISTRY_RE.search(command):
        effects.append("registry_change")
        effects.append("system_change")
        reversible = False
    if _STARTUP_RE.search(command):
        effects.append("startup_change")
        effects.append("system_change")
        reversible = False
    if _PERMISSION_RE.search(command):
        effects.append("permission_change")
        effects.append("system_change")
    if _SECURITY_RE.search(command):
        effects.append("security_change")
        effects.append("system_change")
        reversible = False
    if _CREDENTIAL_RE.search(command):
        effects.append("credential_access")
        effects.append("privacy")
        reversible = False
    if _NETWORK_RE.search(command):
        effects.append("network")
    if _GIT_PUSH_RE.search(command):
        effects.append("external_write")
        effects.append("git_push")
        reversible = None
    if _GIT_FORCE_PUSH_RE.search(command):
        effects.append("git_force_push")
        reversible = False
    if _DROP_RE.search(command):
        effects.append("delete")
        effects.append("external_write")
        reversible = False
    if not effects:
        effects.append("execute")
    return effects, reversible


def _command_paths(command: str) -> list[str]:
    values: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(command):
        token = match.group(0).strip().strip('"').strip("'")
        if len(token) <= 2:
            continue
        values.append(token)
    return values


def _ui_effects(action: str, args: dict[str, Any], element_name: str) -> list[str]:
    effects = [f"ui_{action}"]
    text = str(args.get("text") or "")
    keys = str(args.get("keys") or "")
    label = element_name or str(
        args.get("label") or args.get("target") or args.get("element_name") or ""
    )
    if action == "type" and text:
        if _DELETE_RE.search(text) or _PRIVILEGE_RE.search(text):
            command_effects, _ = _command_effects(text)
            effects.extend(command_effects)
            effects.append("ui_submit")
        elif (
            "\n" in text
            or "\r" in text
            or bool(args.get("press_enter"))
            or bool(args.get("submit"))
        ):
            effects.append("ui_submit")
    if action == "key" and _DESTRUCTIVE_KEYS_RE.search(keys):
        effects.append("ui_destructive")
    if action == "click" and label:
        if _DESTRUCTIVE_UI_RE.search(label):
            effects.append("ui_destructive")
        elif _COMMIT_UI_RE.search(label):
            effects.append("ui_commit")
    if args.get("rollback_risk") or args.get("irreversible"):
        effects.append("ui_commit")
    return list(dict.fromkeys(effects))


def _browser_effects(
    action: str, args: dict[str, Any], element_name: str
) -> list[str]:
    """浏览器动作的效果。语义尽量复用桌面规则，避免两套判断漂移。"""
    if action in READ_ONLY_BROWSER_ACTIONS:
        return ["read"]
    effects: list[str] = [f"browser_{action or 'act'}"]
    if action in {"goto", "navigate", "open"}:
        effects.append("navigate")
        url = str(args.get("url") or "").strip().lower()
        if url and not url.startswith(("http://", "https://", "about:")):
            # file:// javascript: data: 等会跳出普通网页语义
            effects.append("external_write")
    elif action in {"back", "forward", "reload"}:
        effects.append("navigate")
    elif action in {"click", "tap"}:
        effects.extend(_ui_effects("click", args, element_name))
    elif action in {"type", "fill", "input"}:
        effects.extend(_ui_effects("type", args, element_name))
    elif action in {"press", "key"}:
        effects.extend(
            _ui_effects("key", {**args, "keys": args.get("keys") or args.get("text", "")}, element_name)
        )
    if args.get("rollback_risk") or args.get("irreversible"):
        effects.append("ui_commit")
    return list(dict.fromkeys(effects))


def _risk_from_effects(effects: Iterable[str], targets: list[OperationTarget]) -> str:
    effects = set(effects)
    if any(target.hard_blocked for target in targets) and effects & {
        "delete",
        "overwrite",
        "recursive",
        "format",
        "system_change",
    }:
        return "critical"
    if effects & {
        "credential_access",
        "security_change",
        "privilege",
        "format",
        "boot_change",
        "git_force_push",
        "ui_destructive",
    }:
        return "critical"
    if effects & {
        "delete",
        "recursive",
        "overwrite",
        "service_change",
        "registry_change",
        "startup_change",
        "permission_change",
        "system_change",
        "external_write",
        "upload",
        "git_push",
        "ui_commit",
    }:
        return "high"
    if effects & {
        "write",
        "execute",
        "network",
        "download",
        "ui_type",
        "ui_key",
        "ui_submit",
    }:
        return "medium"
    return "low"


def _operation_hash(
    capability: str,
    action: str,
    args: dict[str, Any],
    targets: list[OperationTarget],
) -> str:
    canonical = {
        "capability": capability,
        "action": action,
        "args": _redact(args),
        "targets": [
            {
                "path": _normcase(target.path),
                "exists": target.exists,
                "kind": target.kind,
                "size": target.size_bytes,
            }
            for target in targets
        ],
    }
    raw = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_operation(
    action: Action,
    *,
    observation: Observation | None = None,
    workspace_roots: Iterable[str | Path] | None = None,
    project_root: str | Path | None = None,
    protected_paths: Iterable[str] | None = None,
    hard_block_paths: Iterable[str] | None = None,
) -> OperationDescriptor:
    roots = [
        Path(os.path.realpath(str(root)))
        for root in (workspace_roots or [os.getcwd()])
    ]
    project = Path(os.path.realpath(str(project_root or os.getcwd())))
    protected = list(DEFAULT_PROTECTED_PATHS) + list(protected_paths or [])
    hard_block = list(DEFAULT_HARD_BLOCK_PATHS) + list(hard_block_paths or [])

    raw_args = dict(action.args or {})
    args = clean_args(raw_args)
    capability = action.capability
    ui_action = (
        str(args.get("action", "")).casefold()
        if capability in {"desktop.act", "browser.act", "browser.session"}
        else ""
    )

    path_values: list[str] = []
    effects: list[str] = []
    reversible: bool | None = None

    if capability == "terminal.exec":
        command = str(args.get("command", ""))
        effects, reversible = _command_effects(command)
        if _READ_ONLY_COMMAND_RE.match(command) and not (
            set(effects) & _WRITE_EFFECTS
        ) and ">" not in command:
            effects = ["read"]
            reversible = True
        path_values.extend(_command_paths(command))
        if args.get("cwd"):
            path_values.append(str(args["cwd"]))
    elif capability == "file.transfer":
        direction = str(args.get("direction", "")).casefold()
        for key in ("source", "destination"):
            if args.get(key):
                path_values.append(str(args[key]))
        effects.append("upload" if direction == "upload" else "download")
        effects.append("external_write" if direction == "upload" else "write")
        reversible = direction == "download"
    elif capability == "desktop.act":
        element_name = ""
        element_id = str(args.get("element_id", ""))
        if element_id and observation is not None:
            for element in observation.elements:
                if element.id == element_id:
                    element_name = element.name
                    break
        effects = _ui_effects(ui_action, args, element_name)
    elif capability == "browser.act":
        element_name = ""
        index = args.get("element_index")
        if index is not None and observation is not None:
            wanted = f"b{index}"
            for element in observation.elements:
                if element.id in {wanted, str(index)}:
                    element_name = element.name
                    break
        effects = _browser_effects(ui_action, args, element_name)
    elif capability == "browser.session":
        effects = ["read"] if ui_action in {"status", "handoff"} else ["process_control"]
    else:
        effects = ["read"]

    seen: set[str] = set()
    targets: list[OperationTarget] = []
    for value in path_values:
        path = _normalize_path(value)
        if path is None:
            continue
        key = _normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            _target_info(path, roots, project, protected, hard_block)
        )

    mutating = capability not in READ_ONLY_CAPABILITIES and not (
        capability == "desktop.act" and ui_action in READ_ONLY_DESKTOP_ACTIONS
    ) and not (
        capability == "browser.act" and ui_action in READ_ONLY_BROWSER_ACTIONS
    )
    if capability == "terminal.exec" and effects == ["read"]:
        mutating = False
    if capability == "desktop.act" and ui_action in READ_ONLY_DESKTOP_ACTIONS:
        effects.append("read")
    if capability == "browser.act" and ui_action in READ_ONLY_BROWSER_ACTIONS:
        effects = ["read"]

    risk_hint = _risk_from_effects(effects, targets)
    declared_categories = raw_args.get("sensitive_categories")
    if not isinstance(declared_categories, list):
        declared_categories = []
    declared_sensitive = bool(raw_args.get("sensitive")) or bool(
        declared_categories
    )

    return OperationDescriptor(
        operation_id=_operation_hash(capability, ui_action, args, targets),
        capability=capability,
        action=ui_action or capability.split(".")[-1],
        args=_redact(args),
        targets=targets,
        effects=list(dict.fromkeys(effects)),
        reversible=reversible,
        risk_hint=risk_hint,
        audit_note=str(action.args.get("audit_note", ""))[:1000],
        declared_sensitive=declared_sensitive,
        declared_categories=[str(item)[:40] for item in declared_categories[:8]],
        mutating=mutating,
    )


def detect_sensitivity(
    descriptor: OperationDescriptor,
    *,
    always_confirm: Iterable[str] = (),
) -> Sensitivity:
    capability = descriptor.capability
    effects = set(descriptor.effects)
    always = {str(item) for item in always_confirm}
    reasons: list[str] = []
    categories: list[str] = []
    requires_user = False

    read_only = capability in READ_ONLY_CAPABILITIES or (
        capability == "desktop.act"
        and descriptor.action in READ_ONLY_DESKTOP_ACTIONS
    ) or (
        capability == "browser.act"
        and descriptor.action in READ_ONLY_BROWSER_ACTIONS
    )
    if read_only and not (descriptor.declared_sensitive or capability in always):
        return Sensitivity()

    if capability in READ_ONLY_CAPABILITIES:
        reasons.append("执行模型声明该步骤需要审计")
        categories.append("declared")
    elif capability == "terminal.exec":
        reasons.append("终端命令可在任意位置读写或执行代码")
        categories.extend(["system_change", "data_loss"])
    elif capability == "file.transfer":
        reasons.append("文件传输可能覆盖目标或把数据发送到外部")
        categories.append("external_effect")
        if "write" in effects or "overwrite" in effects:
            categories.append("data_loss")
    elif capability in {"desktop.act", "browser.act"}:
        if "ui_destructive" in effects:
            reasons.append("界面元素或快捷键可能触发删除/卸载/清空")
            categories.append("data_loss")
        elif effects & {
            "delete",
            "recursive",
            "overwrite",
            "privilege",
            "system_change",
            "registry_change",
            "service_change",
            "security_change",
        }:
            reasons.append("输入内容或按键可能执行破坏性命令")
            categories.extend(["system_change", "data_loss"])
    else:
        reasons.append(f"未知能力 {capability} 需要人工判断")
        categories.append("unknown")

    for target in descriptor.targets:
        if target.protected:
            reasons.append(f"受保护路径：{target.path}")
            categories.append("privacy")
            requires_user = True
            if target.exists and target.kind == "file":
                categories.append("data_loss")
        if target.user_data:
            reasons.append(f"用户数据路径：{target.path}")
            categories.extend(["user_data", "privacy"])
            requires_user = True
        if target.hard_blocked:
            reasons.append(f"系统硬拦截路径：{target.path}")
            categories.append("system_change")
        if (
            target.exists
            and not target.inside_project
            and not target.inside_workspace
        ):
            verb = "写入" if descriptor.mutating else "访问"
            reasons.append(f"{verb}当前项目/工作区之外的路径：{target.path}")
            categories.append("cross_project")
            requires_user = True
        if descriptor.mutating and target.protected and target.exists:
            categories.append("data_loss")

    if "credential_access" in effects:
        reasons.append("操作可能读取用户凭据")
        categories.append("privacy")
        requires_user = True

    if descriptor.declared_sensitive:
        reasons.append("执行模型声明该步骤敏感")
        categories.extend(descriptor.declared_categories or ["unknown"])
    if capability in always:
        reasons.append("该能力被配置为必须确认")
        categories.append("policy")

    if not reasons:
        if capability in {"desktop.act", "browser.act"} and descriptor.mutating:
            # Typing and clicking are low-risk by default in AI audit mode.
            return Sensitivity(risk="low")
        return Sensitivity()

    risk = descriptor.risk_hint
    if "critical" in (risk,) or any(
        target.hard_blocked and descriptor.mutating for target in descriptor.targets
    ):
        risk = "critical"
    elif "high" in (risk,) or "credential_access" in effects:
        risk = "high"
    elif risk == "low":
        risk = "medium"

    return Sensitivity(
        sensitive=True,
        categories=tuple(dict.fromkeys(categories)),
        risk=risk,
        reasons=tuple(dict.fromkeys(reasons)),
        hard_blocked=any(
            target.hard_blocked and descriptor.mutating
            for target in descriptor.targets
        ),
        requires_user=requires_user,
    )


def needs_rollback_review(
    descriptor: OperationDescriptor,
    sensitivity: Sensitivity | None = None,
) -> bool:
    """Whether a mistake in this action would be hard or impossible to undo."""
    if not descriptor.mutating:
        return False
    effects = set(descriptor.effects)
    if effects & _ROLLBACK_EFFECTS:
        return True
    if descriptor.reversible is False:
        return True
    if sensitivity is not None and sensitivity.risk in {"high", "critical"}:
        return True
    return False


def needs_visual_review(
    descriptor: OperationDescriptor,
    sensitivity: Sensitivity | None = None,
) -> bool:
    """UI actions lack a textual audit trail, so their review needs an image."""
    return descriptor.capability in {
        "desktop.act",
        "browser.act",
    } and needs_rollback_review(descriptor, sensitivity)


def environment_profile(
    descriptor: OperationDescriptor | None = None,
    *,
    observation: Observation | None = None,
    workspace_roots: Iterable[str | Path] | None = None,
    protected_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    admin = False
    try:
        if os.name == "nt":
            import ctypes

            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    return {
        "os": os.name,
        "platform": platform_module.platform(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "cwd": os.getcwd(),
        "workspace_roots": [str(root) for root in (workspace_roots or [])],
        "protected_patterns": list(protected_paths or []),
        "administrator": admin,
        "active_window": observation.active_window if observation else "",
        "target_kinds": [
            target.kind for target in (descriptor.targets if descriptor else [])
        ],
    }


__all__ = [
    "AUDIT_CONTROL_KEYS",
    "DEFAULT_HARD_BLOCK_PATHS",
    "DEFAULT_PROTECTED_PATHS",
    "OperationDescriptor",
    "OperationTarget",
    "READ_ONLY_CAPABILITIES",
    "Sensitivity",
    "clean_args",
    "detect_sensitivity",
    "environment_profile",
    "needs_rollback_review",
    "needs_visual_review",
    "normalize_operation",
]
