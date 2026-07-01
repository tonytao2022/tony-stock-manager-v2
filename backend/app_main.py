"""
app_main.py - Flask 工厂 + Blueprint 注册
股票智能分析管理系统 v2（全新构建）
"""
import os
import sys
import logging
from flask import Flask, send_from_directory

# 确保能找到同级模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_config import api_success, api_error
from auth import jwt_encode, verify_login, require_auth
from tool_registry import list_tools

# ─── Blueprint 导入 ──────────────────────────────────────────
from routes.dashboard import dashboard_bp
from routes.holdings import holdings_bp
from routes.strategy import strategy_bp
from routes.sector import sector_bp
from routes.analysis import analysis_bp
from routes.system import system_bp
from routes.pipeline import pipeline_bp
from routes.backtest import backtest_bp
from routes.watch_pool import watch_pool_bp
from routes.trade import trade_bp
from routes.dragon import dragon_bp
from routes.legacy import legacy_bp
from routes.ai_analysis import ai_bp


def create_app():
    app = Flask(__name__)
    app.config['JSON_AS_ASCII'] = False

    # ─── 日志 ────────────────────────────────────────────
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(level=getattr(logging, log_level),
                        format='%(asctime)s [%(levelname)s] %(message)s')

    # ─── 健康检查 ────────────────────────────────────────
    @app.route('/health')
    def health():
        try:
            from db_config import db_cursor
            with db_cursor(commit=False) as cur:
                cur.execute("SELECT 1")
                db_ok = cur.fetchone() is not None
            return api_success({'service': 'stock-system-v2', 'port': 8891,
                                'database': 'connected' if db_ok else 'disconnected',
                                'version': '2.0.0'})
        except Exception as e:
            return api_error(str(e), http_status=500)

    # ─── JWT Token 获取（支持密码登录） ──────────────────
    @app.route('/api/v2/auth/token', methods=['POST'])
    def get_token():
        from flask import request
        data = request.get_json(silent=True) or {}
        username = data.get('username', '')
        password = data.get('password', '')
        
        if username and password:
            # 密码登录
            user_info = verify_login(username, password)
            if user_info:
                token = jwt_encode({'sub': user_info['username'], 'role': user_info['role']})
                return api_success({
                    'token': token,
                    'user': user_info['username'],
                    'role': user_info['role'],
                    'display_name': user_info['display_name'],
                })
            else:
                return api_error('用户名或密码错误', http_status=401)
        else:
            # 免密获取临时token（兼容旧前端）
            user = data.get('user', 'tony')
            token = jwt_encode({'sub': user, 'role': 'admin'})
            return api_success({'token': token, 'user': user})

    # ─── 前端静态文件 ────────────────────────────────────
    frontend_dir = os.environ.get('FRONTEND_DIR',
        '/var/www/html/stock-v2')

    @app.route('/')
    def serve_index():
        return send_from_directory(frontend_dir, 'index.html')

    @app.route('/<path:filename>')
    def serve_static(filename):
        return send_from_directory(frontend_dir, filename)

    # ─── 注册 Blueprints ─────────────────────────────────
    app.register_blueprint(dashboard_bp, url_prefix='/api/v2')
    app.register_blueprint(holdings_bp, url_prefix='/api/v2')
    app.register_blueprint(strategy_bp, url_prefix='/api/v2')
    app.register_blueprint(sector_bp, url_prefix='/api/v2')
    app.register_blueprint(analysis_bp, url_prefix='/api/v2')
    app.register_blueprint(system_bp, url_prefix='/api/v2')
    app.register_blueprint(pipeline_bp, url_prefix='/api/v2')
    app.register_blueprint(backtest_bp, url_prefix='/api/v2')
    app.register_blueprint(watch_pool_bp, url_prefix='/api/v2')
    app.register_blueprint(trade_bp, url_prefix='/api/v2')
    app.register_blueprint(dragon_bp, url_prefix='/api/v2')
    app.register_blueprint(legacy_bp, url_prefix='/api/v2')
    app.register_blueprint(ai_bp, url_prefix='/api/v2')

    # ─── API列表（ToolRegistry） ────────────────────────────
    @app.route('/api/v2/system/tools', methods=['GET'])
    @require_auth
    def api_tools():
        return api_success({'tools': list_tools()})

    # ─── CORS ────────────────────────────────────────────
    @app.after_request
    def add_cors(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,X-API-Key,Authorization'
        response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
        return response

    return app


if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', 8891))
    app.run(host='0.0.0.0', port=port, debug=(os.environ.get('DEBUG') == '1'))
