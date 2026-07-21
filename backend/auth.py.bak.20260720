"""
auth.py - 统一认证模块（JWT + X-API-Key 双模）
"""
import os
import hashlib
import time
import json
from functools import wraps
from flask import request, jsonify

from db_config import db_cursor

# ─── 配置 ─────────────────────────────────────────────────
JWT_SECRET = os.environ.get('JWT_SECRET', 'stock-system-v2-secret-key-2026')
JWT_EXPIRY = 86400 * 7  # 7天


# ─── API Key 缓存 ──────────────────────────────────────────
_api_key_cache = {'key': None, 'time': 0}

def _get_api_key():
    now = time.time()
    if _api_key_cache['key'] and now - _api_key_cache['time'] < 300:
        return _api_key_cache['key']

    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT config_value FROM system_config WHERE config_key='api_key' LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                key = row['config_value'] if isinstance(row, dict) else row[0]
                _api_key_cache['key'] = key
                _api_key_cache['time'] = now
                return key
    except:
        pass
    return os.environ.get('API_KEY', '')


# ─── 用户登录校验 ──────────────────────────────────────────
def verify_login(username, password):
    """验证用户名密码，返回用户信息或None"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT id, username, role, display_name FROM sys_users "
                "WHERE username=%s AND password=%s AND is_active=1 LIMIT 1",
                [username, password]
            )
            row = cur.fetchone()
            if row:
                return {
                    'id': row['id'],
                    'username': row['username'],
                    'role': row.get('role', 'user'),
                    'display_name': row.get('display_name', username),
                }
    except:
        pass
    return None


# ─── JWT 工具 ──────────────────────────────────────────────
def jwt_encode(payload):
    """简单JWT编码（无第三方依赖）"""
    header = json.dumps({'alg': 'HS256', 'typ': 'JWT'})
    b64_header = _base64url(header.encode())
    payload['iat'] = int(time.time())
    payload['exp'] = int(time.time()) + JWT_EXPIRY
    b64_payload = _base64url(json.dumps(payload).encode())
    signature = _base64url(
        hashlib.sha256(f'{b64_header}.{b64_payload}.{JWT_SECRET}'.encode()).hexdigest()
        .encode()
    )
    return f'{b64_header}.{b64_payload}.{signature}'


def jwt_decode(token):
    """解码验证JWT"""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        payload_bytes = _base64url_decode(parts[1])
        payload = json.loads(payload_bytes)
        if payload.get('exp', 0) < time.time():
            return None
        expected_sig = _base64url(
            hashlib.sha256(f'{parts[0]}.{parts[1]}.{JWT_SECRET}'.encode())
            .hexdigest().encode()
        )
        if parts[2] != expected_sig:
            return None
        return payload
    except:
        return None


def _base64url(data):
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _base64url_decode(data):
    import base64
    padding = 4 - len(data) % 4
    if padding != 4:
        data += '=' * padding
    return base64.urlsafe_b64decode(data)


# ─── 认证装饰器 ────────────────────────────────────────────
def require_auth(f):
    """双重认证：JWT Bearer Token 或 X-API-Key"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 健康检查不验证
        if request.path in ('/health', '/api/v2/system/health'):
            return f(*args, **kwargs)
        if request.method == 'OPTIONS':
            return f(*args, **kwargs)

        auth_header = request.headers.get('Authorization', '')
        api_key_header = request.headers.get('X-API-Key', '')

        # 1. JWT 验证优先
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            payload = jwt_decode(token)
            if payload:
                request.user_id = payload.get('sub', 'system')
                return f(*args, **kwargs)

        # 2. JWT Token 通过 X-API-Key 头传入（前端 `token:` 前缀）
        if api_key_header.startswith('token:'):
            token = api_key_header[6:]
            payload = jwt_decode(token)
            if payload:
                request.user_id = payload.get('sub', 'system')
                return f(*args, **kwargs)

        # 3. 普通 X-API-Key 验证
        if api_key_header:
            expected = _get_api_key()
            if api_key_header == expected:
                request.user_id = 'system'
                return f(*args, **kwargs)

        return jsonify({'code': 401, 'message': '认证失败', 'data': None}), 401

    return decorated
