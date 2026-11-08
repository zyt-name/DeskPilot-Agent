from __future__ import annotations

from .manager import ResourceManager, ResourceState
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
    "resource_by_id",
]
