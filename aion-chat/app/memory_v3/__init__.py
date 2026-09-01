"""Memory V3 infrastructure.

The package is deliberately inert until an explicit feature flag enables a
read or write path.  Importing it may create/upgrade schema through
``database.init_db`` but must not change prompt contents.
"""

from .config import load_memory_v3_config, normalize_memory_v3_config

__all__ = ["load_memory_v3_config", "normalize_memory_v3_config"]
