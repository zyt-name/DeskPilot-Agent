from __future__ import annotations

from .manager import ResourceManager, ResourceState
from .updates import UpdateCandidate, UpdateChecker
from .manifest import (
    GITHUB_ARCHIVE_MIRRORS,
    NPM_REGISTRIES,
    PYPI_INDEXES,
    RESOURCES,
    Resource,
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
    "resource_by_id",
]
