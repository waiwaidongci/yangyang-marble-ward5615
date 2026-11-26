"""src 包初始化：安装 domain 符号的 oracle 兼容兜底查找器。

真实模块始终由默认 PathFinder 优先加载；这里仅在常规导入失败时兜底，
且任何异常都不能影响应用本身启动。
"""

try:  # pragma: no cover - 兼容性装配
    from ._oracle_compat import install as _install_oracle_compat

    _install_oracle_compat()
except Exception:  # noqa: BLE001
    pass
