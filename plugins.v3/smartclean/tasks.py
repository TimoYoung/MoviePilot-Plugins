"""兼容旧导入路径。新代码请使用 libraries / rules。"""

from .libraries import library_owner, normalize_library as normalize_task

__all__ = ["library_owner", "normalize_task"]
