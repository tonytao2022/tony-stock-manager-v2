"""
tool_registry.py - 工具注册表（参考Claude Code Tool.ts设计）

每个API端点注册为标准化工具，包含：
- name / description: 能力描述
- input_schema / output_schema: 输入输出结构
- is_readonly: 是否只读（安全标记）
- is_concurrency_safe: 是否可并发
- cache_ttl: 缓存建议秒数
- destructive_level: 破坏性等级(0-3)
"""
import time
from functools import wraps

# ─── 注册表 ───────────────────────────────────────────────────
_registry = {}


def register_tool(name, **kwargs):
    """注册一个工具（装饰器）"""
    def decorator(func):
        defaults = {
            'name': name,
            'description': '',
            'input_schema': {},
            'output_schema': {},
            'is_readonly': True,
            'is_concurrency_safe': True,
            'cache_ttl': 0,          # 0=不缓存
            'destructive_level': 0,  # 0=安全, 1=写操作, 2=危险写, 3=需确认
        }
        defaults.update(kwargs)
        _registry[name] = defaults

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


def list_tools():
    """列出所有已注册的工具"""
    return list(_registry.values())


def get_tool(name):
    """获取单个工具定义"""
    return _registry.get(name)
