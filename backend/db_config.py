"""
db_config.py — 数据库配置 + Tushare Token + 响应工具
统一响应格式: {code, message, data, error, timestamp, request_id}
"""
import os
import uuid
import pymysql
from datetime import datetime, date
from contextlib import contextmanager
from flask import jsonify

# ─── 密码获取（优先环境变量）──────────────────────────────────
_db_password = None

def _get_password():
    global _db_password
    if _db_password is not None:
        return _db_password
    # 1. 环境变量优先
    pwd = os.environ.get('MYSQL_PASS')
    if pwd:
        _db_password = pwd
        return _db_password
    # 2. fallback: 读 /etc/mysql/debian.cnf
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if 'password' in line:
                    _db_password = line.strip().split('=')[-1].strip().strip('"').strip("'")
                    break
    except Exception:
        _db_password = os.environ.get('MYSQL_PASSWORD', '')
    return _db_password


def _get_db_config():
    """动态获取数据库配置"""
    return {
        'host': os.environ.get('DB_HOST', '127.0.0.1'),
        'port': int(os.environ.get('DB_PORT', 3306)),
        'user': os.environ.get('DB_USER', 'debian-sys-maint'),
        'password': _get_password(),
        'database': os.environ.get('DB_NAME', 'stock_db_v2'),
        'charset': 'utf8mb4',
        'connect_timeout': 5,
        'cursorclass': pymysql.cursors.DictCursor,
        'autocommit': True,
    }


# 保留 DB_CONFIG 变量名兼容性（旧代码 from db_config import DB_CONFIG）
DB_CONFIG = None  # 标记为已废弃，实际使用 _get_db_config()


_connection_pool = {}
def get_connection():
    """返回新的数据库连接（多线程安全：不缓存连接）"""
    cfg = _get_db_config()
    conn = pymysql.connect(**cfg)
    return conn


@contextmanager
def db_cursor(commit=True):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_tushare_token():
    return os.environ.get('TUSHARE_TOKEN', '')


# ─── 统一响应工具 ────────────────────────────────────────────
def api_success(data=None, message="success", code=0):
    """统一成功响应"""
    return jsonify({
        "code": code,
        "message": message,
        "data": data if data is not None else {},
        "error": None,
        "timestamp": datetime.now().isoformat(),
        "request_id": str(uuid.uuid4())[:8]
    })

def api_error(error="unknown error", code=-1, message=None, http_status=500):
    """统一错误响应: code=-1表示数据异常"""
    return jsonify({
        "code": code,
        "message": message or error,
        "data": None,
        "error": error,
        "timestamp": datetime.now().isoformat(),
        "request_id": str(uuid.uuid4())[:8]
    }), http_status

def api_not_found():
    return api_error("数据不存在", code=2001, http_status=404)


# ─── 序列化 ──────────────────────────────────────────────────
def serialize_rows(rows, float_fields=None):
    """序列化行数据，自动处理 date/datetime/Decimal/bytes 类型
    
    Args:
        rows: pymysql DictCursor 返回的行列表
        float_fields: 可选，指定哪些字段强制转 float（如 cost_price, current_price 等）
    """
    from decimal import Decimal
    result = []
    for row in rows:
        item = {}
        for k, v in row.items():
            if isinstance(v, (date, datetime)):
                item[k] = v.isoformat()
            elif isinstance(v, bytes):
                item[k] = v.decode('utf-8')
            elif isinstance(v, Decimal):
                item[k] = float(v)
            else:
                item[k] = v
        
        # 若有指定float_fields，额外强转
        if float_fields:
            for f in float_fields:
                if f in item and item[f] is not None:
                    item[f] = float(item[f])
        result.append(item)
    return result


# 兼容旧名
_serialize_rows = serialize_rows

# ─── 用户ID管理 ────────────────────────────────────────────
def get_default_user():
    """从环境变量获取默认用户，替代原有硬编码'tony'"""
    return os.environ.get('STOCK_USER', 'tony')

def get_user_id():
    """从system_config获取默认用户ID，硬编码统一入口"""
    try:
        cur = _get_cursor()
        cur.execute("SELECT config_value FROM system_config WHERE config_key='default_user_id' LIMIT 1")
        r = cur.fetchone()
        cur.close()
        if r:
            v = r['config_value'] if isinstance(r, dict) else r[0]
            if v: return v
    except Exception as e:
        pass
    return get_default_user()

def _get_cursor():
    """内部获取游标，不依赖flask上下文"""
    conn = get_connection()
    return conn.cursor(pymysql.cursors.DictCursor)

# 兼容旧名（alias）
get_user_id_old = get_user_id

# ─── 铁律: 数据标记 + 重试 ────────────────────────────────────
DATA_ERROR_MARKER = -1
# -1标记的数据不可参与评分/回测计算
# API失败→等15秒→重试3次→仍失败置为-1并报警

