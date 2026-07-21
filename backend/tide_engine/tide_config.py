#!/usr/bin/env python3
"""
tide_config.py - Tide评分引擎配置管理
权重/参数从 tide_config 表读取，不硬编码
"""
import json, os, sys
from typing import Dict, Optional

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from db_config import get_connection

_DEFAULT_WEIGHTS = {
    'f1': 0.10, 'f3': 0.15, 'f4': 0.48,
    'f5': 0.15, 'f6': 0.12,
}


def get_factor_weights() -> Dict[str, float]:
    """从 DB 读取因子权重，失败返回默认值"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT config_value FROM tide_config WHERE config_key='factor_weights'")
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            return json.loads(row['config_value'])
    except Exception as e:
        print(f"[tide_config] 读取权重失败: {e}")
    return dict(_DEFAULT_WEIGHTS)


def get_config_bool(key: str, default: bool = True) -> bool:
    """读取布尔型配置"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT config_value FROM tide_config WHERE config_key=%s", (key,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            return row['config_value'].lower() in ('true', '1', 'yes')
    except Exception as e:
        pass  # 非关键：配置读取失败，使用默认值
    return default
