from __future__ import annotations

from .manager import ResourceManager, ResourceState
from .updates import UpdateCandidate, UpdateChecker
from .upgrade import (
    UpgradeResult,
    Upgrader,
    make_prober,
    reason_of,
    relocate,
)
from .manifest import (
    GITHUB_ARCHIVE_MIRRORS,
    NPM_REGISTRIES,
    PYPI_INDEXES,
    RESOURCES,
    Resource,
    clean_version,
    resource_by_id,
)

__all__ = [
    "GITHUB_ARCHIVE_MIRRORS",
    "NPM_REGISTRIES",
    "PYPI_INDEXES",
    "RESOURCES",
    "Resource",
    "ResourceManager",
    "ResourceState",
    "UpdateCandidate",
    "UpdateChecker",
    "UpgradeResult",
    "Upgrader",
    "clean_version",
    "make_prober",
    "reason_of",
    "relocate",
    "resource_by_id",
]
