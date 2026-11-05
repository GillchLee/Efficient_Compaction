"""Compatibility shim: the Harbor adapter moved to optlimit.adapters.harbor_terminus2.

Job configs keep working with `import_path: optlimit.harbor_agent:OptlimitTerminus2`.
"""
from .adapters.harbor_terminus2 import OptlimitTerminus2

__all__ = ["OptlimitTerminus2"]
