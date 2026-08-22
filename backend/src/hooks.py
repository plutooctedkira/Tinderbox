"""Hook / 插件系统。

核心代码通过 trigger() 在关键节点广播事件；第三方插件通过 register()
挂载回调。核心逻辑不感知任何具体插件——增删 plugins/ 目录下的文件即可，
无需改动核心代码。

支持的事件（见 EVENTS）：
  memory_inserted   记忆插入后
  memory_superseded 版本更迭后
  memory_retrieved  检索命中后
  memory_decayed    每日衰减后
"""

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("memory.hooks")

# 可注册的事件。register() 会校验事件名。
EVENTS = (
    "memory_inserted",
    "memory_superseded",
    "memory_retrieved",
    "memory_decayed",
)

_HOOKS = {e: [] for e in EVENTS}


def register(event, callback):
    """注册一个 hook 回调。回调用 **kwargs 接收事件参数。"""
    if event not in EVENTS:
        raise ValueError(f"未知 hook 事件: {event}（可选: {', '.join(EVENTS)}）")
    _HOOKS[event].append(callback)


def trigger(event, **kwargs):
    """触发事件，逐个调用回调。回调异常只记日志，绝不阻断主流程。"""
    for cb in _HOOKS.get(event, []):
        try:
            cb(**kwargs)
        except Exception as e:
            logger.warning(
                "hook [%s] 回调 %s 失败: %s",
                event, getattr(cb, "__name__", repr(cb)), e,
            )


def load_plugins(plugin_dir=None):
    """加载 plugins/ 目录下所有插件模块，返回加载数量。

    每个插件是一个 .py 文件，须导出 `register(registry)` 函数：
    registry 即本模块的 register()，插件用它注册感兴趣的事件。
    加载失败只记日志，不影响核心启动。
    """
    if plugin_dir is None:
        plugin_dir = Path(__file__).resolve().parent.parent / "plugins"
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return 0

    loaded = 0
    for f in sorted(plugin_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"memory_plugin_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "register"):
                logger.warning("插件 %s 缺少 register() 函数，跳过", f.name)
                continue
            mod.register(register)
            loaded += 1
            logger.info("插件已加载: %s", f.name)
        except Exception as e:
            logger.error("插件 %s 加载失败: %s", f.name, e)
    return loaded
