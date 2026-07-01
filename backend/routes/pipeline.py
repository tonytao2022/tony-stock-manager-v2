"""
routes/pipeline.py - 管道管理
"""
import os, subprocess, json, time
from flask import Blueprint
from db_config import db_cursor, api_success, api_error, serialize_rows
from auth import require_auth

pipeline_bp = Blueprint('pipeline', __name__)


@pipeline_bp.route('/pipeline/status', methods=['GET'])
@require_auth
def pipeline_status():
    """管道状态"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT * FROM pipeline_exec_log
                ORDER BY id DESC LIMIT 20
            """)
            logs = cur.fetchall()

        # 检查锁
        lock_file = '/tmp/stock_pipeline_v2.lock'
        is_running = False
        if os.path.exists(lock_file):
            try:
                pid = open(lock_file).read().strip()
                if pid.isdigit() and os.path.isdir(f'/proc/{pid}'):
                    is_running = True
            except:
                pass

        return api_success({
            'is_running': is_running,
            'lock_file': lock_file,
            'recent_logs': serialize_rows(logs),
        })
    except Exception as e:
        return api_error(str(e))


@pipeline_bp.route('/pipeline/trigger', methods=['POST'])
@require_auth
def trigger_pipeline():
    """手动触发管道"""
    try:
        pipeline_script = os.environ.get(
            'PIPELINE_SCRIPT',
            '/root/stock-system-v2/backend/pipelines/daily_orch.py'
        )

        if not os.path.exists(pipeline_script):
            return api_error(f'管道脚本不存在: {pipeline_script}')

        # 后台运行
        result = subprocess.run(
            ['python3', pipeline_script, '--manual'],
            capture_output=True, text=True, timeout=300
        )

        return api_success({
            'exit_code': result.returncode,
            'stdout': result.stdout[-1000:] if result.stdout else '',
            'stderr': result.stderr[-500:] if result.stderr else '',
        })
    except subprocess.TimeoutExpired:
        return api_error('管道执行超时(>300秒)')
    except Exception as e:
        return api_error(str(e))
