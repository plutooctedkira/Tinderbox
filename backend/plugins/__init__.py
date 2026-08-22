"""插件目录。

每个 .py 文件是一个插件，须导出 `register(registry)` 函数；
registry 是 hooks.register，用它注册感兴趣的事件。

新增插件：复制 example_plugin.py 改成自己的逻辑即可。
卸载插件：直接删除对应 .py 文件，核心代码零改动。
"""
