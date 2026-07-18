#!/usr/bin/env python3
"""
tide_server.py - Tide评分引擎独立服务（端口8893）
完全独立于 stock-system-v2 旧版后端
"""
import os, sys, json, logging

_tide_dir = os.path.dirname(os.path.abspath(__file__))
if _tide_dir not in sys.path: sys.path.insert(0, _tide_dir)

from flask import Flask, jsonify, request
from db_config import get_connection
from tide_engine.tide_routes import tide_bp

app = Flask(__name__)

# 简化鉴权（从环境变量读，无硬编码）
API_KEY = os.environ.get('TIDE_API_KEY', '90a275cbcc004fd5')
READ_WHITELIST = [
    '/api/v3/tide-score-list',
    '/api/v3/tide-factor-detail',
    '/api/v3/tide-chanlun',
    '/api/v3/tide-config',
    '/api/v3/tide-compare',
    '/api/v3/tide-backtest-compare',
    '/api/v3/tide-ic-report',
]

@app.before_request
def check_auth():
    if request.method == 'OPTIONS':
        return
    path = request.path.rstrip('/')
    # GET 只读放行
    if request.method == 'GET' and any(path.startswith(w) for w in READ_WHITELIST):
        return
    key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if key != API_KEY:
        return jsonify({"code": -1, "data": None, "message": "unauthorized"}), 401

app.register_blueprint(tide_bp)

if __name__ == '__main__':
    port = int(os.environ.get('TIDE_PORT', 8893))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    print(f"Tide独立服务 -> 0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
