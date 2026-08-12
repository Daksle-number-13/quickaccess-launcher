"""QuickAccess launcher core package."""

from .models import AppearanceMode, LauncherConfig, LauncherItem
from .storage import ConfigStore, LoadResult

__version__ = "1.0.0"

__all__ = [
    "ConfigStore",
    "AppearanceMode",
    "LauncherConfig",
    "LauncherItem",
    "LoadResult",
    "__version__",
]
