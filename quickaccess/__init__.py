"""QuickAccess launcher core package."""

from .models import AppearanceMode, ItemType, LauncherConfig, LauncherItem, normalize_web_url
from .storage import ConfigStore, LoadResult

__version__ = "2.0.0"
__author__ = "Daksle"

__all__ = [
    "ConfigStore",
    "AppearanceMode",
    "ItemType",
    "LauncherConfig",
    "LauncherItem",
    "LoadResult",
    "normalize_web_url",
    "__author__",
    "__version__",
]
