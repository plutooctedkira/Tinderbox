"""示例插件：演示如何通过 register(registry) 挂载 hook。

把它复制成新文件即可新增插件；删除文件即可卸载插件，核心代码零改动。
"""

import logging

logger = logging.getLogger("plugin.example")


def register(registry):
    """registry 是 hooks.register，用它注册感兴趣的事件。"""

    def on_memory_inserted(entry_id, category, content, **kw):
        logger.info("[example] 新记忆插入: %s [%s] %s",
                    entry_id, category, content[:50])

    def on_memory_decayed(stats, **kw):
        logger.info("[example] 每日衰减完成: %s", stats)

    registry("memory_inserted", on_memory_inserted)
    registry("memory_decayed", on_memory_decayed)
