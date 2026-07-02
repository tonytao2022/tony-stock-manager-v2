#!/usr/bin/env python3
"""V2统一API"""
import os, json, subprocess, re
from flask import Flask, jsonify, request
import pymysql

def conn():
    p = re.search(r'password\s*=\s*(\S+)', open('/etc/mysql/debian.cnf').read()).group(1)
    return pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=p, database="stock_db_v2", charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# ====== API Key 认证 ======
API_KEY = os.environ.get('V2_API_KEY', '90a275cbcc004fd5')
WHITELIST = ['/api/v2/health', '/api/v2/system/health']

def ok(d):
    return jsonify({"code": 0, "data": d, "message": "success"})
def err(m):
    return jsonify({"code": -1, "data": None, "message": m})

@app.before_request
def check_api_key():
    if request.method == 'OPTIONS':
        return
    path = request.path.rstrip('/')
    if path in WHITELIST or any(path.startswith(w) for w in WHITELIST):
        return
    key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if key != API_KEY:
        return jsonify({"code": -1, "data": None, "message": "unauthorized"}), 401

@app.route('/api/v2/auth/token', methods=['POST'])
def auth():
    d = request.get_json() or {}
    if d.get('username') and d.get('password'):
        return ok({'token': 'v2-token', 'user': d['username'], 'display_name': 'V2User', 'role': 'admin'})
    return err('invalid')

@app.route('/api/v2/dashboard')
def dash():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
    td = str(cu.fetchone()[0])
    cu.execute("SELECT season, hengjiyuan_level, raw_score, regime FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
    sr = cu.fetchone()
    mkt = {'season': sr[0], 'hengji': sr[1] or 'weak_heng', 'score': float(sr[2] or 0), 'regime': sr[3] or 'range'} if sr else {}
    cu.execute("SELECT ss.ts_code, sb.name, ss.composite_score, ss.calibrated_score, ss.direction, ss.signal_label, ss.season FROM strategy_signal ss LEFT JOIN stock_basic sb ON ss.ts_code=sb.ts_code WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0 ORDER BY ss.composite_score DESC LIMIT 5", (td,))
    t5 = []
    for r in cu.fetchall():
        n = r[1] if r[1] else ''
        t5.append({'ts_code': r[0], 'name': n, 'score': float(r[2] or 0), 'calibrated_score': float(r[3]) if r[3] else None, 'direction': r[4] or '', 'signal_label': r[5] or '', 'season': r[6] or ''})
    cu.execute("SELECT CASE WHEN composite_score>=75 THEN 'strong_buy' WHEN composite_score>=60 THEN 'buy' WHEN composite_score>=40 THEN 'hold' ELSE 'wait' END as st,COUNT(*) FROM strategy_signal WHERE trade_date=%s AND is_calculable=1 AND gate_triggered=0 GROUP BY st", (td,))
    sd = dict(cu.fetchall())
    cu.execute("SELECT ts_code, name, shares, profit_pct, market_value FROM portfolio_holdings WHERE status='HOLDING'")
    hs = []
    for r in cu.fetchall():
        n = r[1] if r[1] else ''
        hs.append({'ts_code': r[0], 'name': n, 'shares': int(r[2] or 0), 'profit_pct': float(r[3] or 0), 'market_value': float(r[4] or 0)})
    cu.close(); c.close()
    return ok({'trade_date': td, 'market': mkt, 'top5': t5, 'signal_distribution': sd, 'holdings': hs})

@app.route('/api/v2/strategy/signals')
def signals():
    limit = int(request.args.get('limit', 0))
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 50))
    c = conn(); cu = c.cursor()
    cu.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
    td = str(cu.fetchone()[0])
    
    # 先取总数
    cu.execute("SELECT COUNT(*) as total FROM strategy_signal ss WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0", (td,))
    total = cu.fetchone()[0]
    
    sql = "SELECT ss.*, sb.name, sb.industry FROM strategy_signal ss LEFT JOIN stock_basic sb ON ss.ts_code=sb.ts_code WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0 ORDER BY ss.composite_score DESC"
    
    if limit > 0:
        # 兼容旧接口：limit指定行数
        sql += " LIMIT " + str(limit)
    else:
        # 新分页逻辑
        offset = (page - 1) * page_size
        sql += " LIMIT %d OFFSET %d" % (page_size, offset)
    
    cu.execute(sql, (td,))
    cols = [d[0] for d in cu.description]
    sigs = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    for s in sigs:
        for k in ['composite_score','calibrated_score','raw_score','trend_score','momentum_score','structure_score','emotion_score','tiger_confidence','position_pct']:
            if k in s and s[k] is not None:
                try: s[k] = float(s[k])
                except: pass
    return ok({'trade_date': td, 'total': total, 'page': page, 'page_size': page_size, 'count': len(sigs), 'signals': sigs})

@app.route('/api/v2/strategy/checkpoints')
def checkpoints():
    from datetime import date
    c = conn(); cu = c.cursor()
    cu.execute("SELECT ph.*, ss.composite_score as score FROM portfolio_holdings ph LEFT JOIN strategy_signal ss ON ph.ts_code=ss.ts_code AND ss.trade_date=(SELECT MAX(trade_date) FROM strategy_signal) WHERE ph.status='HOLDING'")
    cols = [d[0] for d in cu.description]
    rows = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    today = date.today()
    cps = []
    for r in rows:
        bd = r.get('buy_date')
        hold_days = (today - bd).days if bd else 0
        pp = float(r.get('profit_pct') or 0)
        score = float(r.get('score') or 0)
        # 简单判断
        if pp <= -10:
            action, al, reason = 'SELL', '🛑 止损', f'亏损{pp:.1f}%，触发止损'
        elif hold_days >= 30:
            action, al, reason = 'SELL', '⏰ 到期', f'持有{hold_days}日达上限'
        elif score < 20 and hold_days >= 20:
            action, al, reason = 'SELL', '🔴 评分低', f'评分{score}低于20'
        elif pp > 15 and hold_days >= 10:
            action, al, reason = 'HOLD', '💰 止盈观察', f'盈利{pp:.1f}%'
        else:
            action, al, reason = 'HOLD', '🟢 持有', f'评分{score}持有{hold_days}日'
        cps.append({
            'ts_code': r.get('ts_code'),
            'name': r.get('name'),
            'hold_days': hold_days,
            'profit_pct': pp,
            'score': score,
            'cost_price': float(r.get('cost_price') or 0),
            'current_price': float(r.get('current_price') or 0),
            'shares': int(r.get('shares') or 0),
            'market_value': float(r.get('market_value') or 0),
            'action': action,
            'action_label': al,
            'reason': reason,
            'status': r.get('status'),
        })
    return ok({'count': len(cps), 'checkpoints': cps})

@app.route('/api/v2/strategy/config')
def sconfig():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT id, name, season_type, buy_min_score, max_pos_pct, max_total_pct, position_tolerance, stop_loss_pct, trailing_stop_pct, cool_days, max_hold_days, p1_score, p2_score, p3_score, description FROM strategy_config WHERE 1=1 ORDER BY id")
    cols = [d[0] for d in cu.description]
    cfg = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok(cfg)

@app.route('/api/v2/holdings', methods=['GET', 'POST'])
def holdings():
    if request.method == 'POST':
        return add_holding()
    c = conn(); cu = c.cursor()
    st = request.args.get('status', '')
    sql = "SELECT ph.*, ss.composite_score as score, ss.signal_label, dk.close as current_price, dk.change_pct FROM portfolio_holdings ph LEFT JOIN strategy_signal ss ON ph.ts_code=ss.ts_code AND ss.trade_date=(SELECT MAX(trade_date) FROM strategy_signal) LEFT JOIN daily_kline_qfq dk ON ph.ts_code=dk.ts_code AND dk.trade_date=(SELECT MAX(trade_date) FROM daily_kline_qfq)"
    if st:
        sql += " WHERE ph.status='" + st + "'"
    sql += " ORDER BY ph.created_at DESC"
    cu.execute(sql)
    cols = [d[0] for d in cu.description]
    rows = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    # 数值字段转float + 用K线最新价覆盖current_price
    for r in rows:
        for k in ['cost_price','current_price','profit_pct','profit_amount','market_value','position_ratio','score']:
            if k in r and r[k] is not None:
                try: r[k] = float(r[k])
                except: pass
        # 用K线最新收盘价覆盖current_price，重新计算market_value和profit
        kline_price = r.get('current_price')  # 来自daily_kline_qfq
        cp = float(r.get('cost_price') or 0)
        shares = int(r.get('shares') or 0)
        if kline_price and kline_price > 0 and shares > 0:
            r['current_price'] = float(kline_price)
            r['market_value'] = round(kline_price * shares, 2)
            if cp > 0:
                r['profit_pct'] = round((kline_price - cp) / cp * 100, 2)
                r['profit_amount'] = round((kline_price - cp) * shares, 2)
    return ok({'holdings': rows})

def add_holding():
    try:
        d = request.get_json() or {}
        ts_code = d.get('ts_code', '')
        name = d.get('name', '')
        shares = int(d.get('shares', 0))
        cost_price = float(d.get('cost_price', 0))
        buy_date = d.get('buy_date', '')
        if not ts_code or shares <= 0:
            return err('参数不完整')
        c = conn(); cu = c.cursor()
        cu.execute("INSERT INTO portfolio_holdings (ts_code, name, shares, cost_price, buy_date, status) VALUES (%s, %s, %s, %s, %s, 'HOLDING')",
                   (ts_code, name, shares, cost_price, buy_date))
        c.commit()
        cu.close(); c.close()
        try:
            from trade_manager import sync_to_portfolio
            sync_to_portfolio(ts_code, None)
        except:
            pass
        return ok({'status': 'ok', 'ts_code': ts_code})
    except Exception as e:
        return err(str(e))


@app.route('/api/v2/holdings/<ts_code>', methods=['DELETE'])
def del_holding(ts_code):
    try:
        c = conn(); cu = c.cursor()
        cu.execute("DELETE FROM portfolio_holdings WHERE ts_code=%s", (ts_code,))
        c.commit()
        cu.close(); c.close()
        return ok({'status': 'deleted', 'ts_code': ts_code})
    except Exception as e:
        return err(str(e))


@app.route('/api/v2/holdings/calc', methods=['GET', 'POST'])


def calc_h():
    try:
        import os, sys
        sys.path.insert(0, os.path.dirname(__file__))
        from trade_manager import sync_to_portfolio
        c = conn(); cu = c.cursor()
        cu.execute("SELECT ts_code FROM portfolio_holdings WHERE status='HOLDING'")
        codes = [r[0] for r in cu.fetchall()]
        cu.close(); c.close()
        for code in codes:
            sync_to_portfolio(code, None)
        return ok({'status': 'ok', 'synced': len(codes)})
    except Exception as e:
        return err(str(e))

@app.route('/api/v2/backtest/pool')
def bpool():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE' ORDER BY name")
    stks = []
    for r in cu.fetchall():
        n = r[1] if r[1] else ''
        stks.append({'ts_code': r[0], 'name': n})
    cu.close(); c.close()
    return ok({'stocks': stks, 'total': len(stks)})

@app.route('/api/v2/backtest/run', methods=['POST'])
def brun():
    return ok({'status': 'ok', 'message': '回测由定时管道执行'})

@app.route('/api/v2/backtest/history')
def bhist():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT id, report_date, strategy_name, total_trades, win_trades, lose_trades, win_rate, avg_win_pct, avg_lose_pct, profit_factor, max_drawdown, total_return, avg_hold_days FROM backtest_report ORDER BY report_date DESC")
    cols = [d[0] for d in cu.description]
    runs = [dict(zip(cols, r)) for r in cu.fetchall()]
    for r in runs:
        for k in ['win_rate','avg_win_pct','avg_lose_pct','profit_factor','max_drawdown','total_return','avg_hold_days']:
            if k in r and r[k] is not None: r[k] = float(r[k])
    cu.close(); c.close()
    return ok({'runs': runs, 'total': len(runs)})

@app.route('/api/v2/backtest/report/<int:report_id>')
def backtest_report_detail(report_id):
    c = conn(); cu = c.cursor()
    cu.execute("SELECT * FROM backtest_report WHERE id=%s", (report_id,))
    cols = [d[0] for d in cu.description]
    row = cu.fetchone()
    cu.close(); c.close()
    if not row:
        return err('报告不存在')
    report = dict(zip(cols, row))
    # 字段转类型
    for k in ['win_rate','avg_win_pct','avg_lose_pct','profit_factor','max_drawdown','total_return','avg_hold_days']:
        if report.get(k) is not None: report[k] = float(report[k])
    if report.get('total_return') is not None: report['total_return'] = float(report['total_return'])
    # trade_records是JSON
    import json as _j
    if report.get('trade_records') and isinstance(report['trade_records'], str):
        report['trade_records'] = _j.loads(report['trade_records'])
    elif report.get('trade_records') and isinstance(report['trade_records'], bytes):
        report['trade_records'] = _j.loads(report['trade_records'].decode())
    return ok(report)


@app.route('/api/v2/system/health')
def shealth():
    try:
        c = conn(); cu = c.cursor()
        cu.execute("SELECT 1"); cu.close(); c.close()
        db_ok = True
    except:
        db_ok = False
    disk = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
    dp = disk.stdout.split('\n')[1].split() if disk.stdout else [''] * 6
    return ok({'service': 'v2-unified', 'port': 8891, 'status': 'running',
               'database': 'connected' if db_ok else 'disconnected',
               'disk_usage': dp[4] if len(dp) > 4 else 'unknown'})

@app.route('/api/v2/system/config')
def sconfig2():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT id, config_key, config_value, description FROM system_config ORDER BY id")
    cols = [d[0] for d in cu.description]
    cfg = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok(cfg)

@app.route("/api/v2/sector/top")
def sector_top():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT MAX(trade_date) as d FROM sector_index_daily")
    td = str(cu.fetchone()['d'])
    cu.execute("""
        SELECT s.sector_code, s.change_pct
        FROM sector_index_daily s
        WHERE s.trade_date = %s
        ORDER BY ABS(s.change_pct) DESC LIMIT 20
    """, (td,))
    sec = cu.fetchall()
    from_index = []
    for s in sec:
        pct = float(s['change_pct'] or 0)
        # 统计该板块的股票数
        cu.execute("SELECT COUNT(*) as c FROM stock_basic WHERE industry=%s AND is_active=1", (s['sector_code'],))
        stock_cnt = int(cu.fetchone()['c'] or 0)
        # 趋势判定：涨幅>2%为强势，>0为偏强，<0为弱势
        if pct > 2: trend = 'up'
        elif pct > 0: trend = 's_up'
        elif pct > -2: trend = 's_down'
        else: trend = 'down'
        from_index.append({
            "sector_name": s['sector_code'],
            "change_pct": pct,
            "trend_type": trend,
            "structure_score": 50,
            "stock_count": stock_cnt,
        })
    cu.close(); c.close()
    return ok({"from_index": from_index, "from_chanlun": []})

@app.route('/api/v2/sector/rotation')
def srotation():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT sector_code, sector_code, trade_date, change_pct, vol FROM sector_index_daily WHERE trade_date=(SELECT MAX(trade_date) FROM sector_index_daily) ORDER BY pct_change DESC LIMIT 30")
    cols = [d[0] for d in cu.description]
    sec = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok({'sectors': sec})

@app.route('/api/v2/health')
def health():
    return jsonify({"status": "ok", "version": "v2-unified", "database": "stock_db_v2"})




@app.route("/api/v2/system/data-dictionary")
def data_dictionary():
    c = conn(); cu = c.cursor()
    cat = request.args.get('category', '')
    if cat:
        cu.execute("SELECT id, category, en_key, cn_value, description, sort_order FROM data_dictionary WHERE category=%s ORDER BY sort_order ASC", (cat,))
    else:
        cu.execute("SELECT id, category, en_key, cn_value, description, sort_order FROM data_dictionary ORDER BY category, sort_order ASC")
    cols = [d[0] for d in cu.description]
    items = [dict(zip(cols, r)) for r in cu.fetchall()]
    
    # 按category分组
    grouped = {}
    for item in items:
        g = item['category']
        if g not in grouped:
            grouped[g] = {'category': g, 'items': []}
        grouped[g]['items'].append(item)
    
    cu.close(); c.close()
    return ok({'items': items, 'grouped': list(grouped.values()), 'total': len(items)})
@app.route("/api/v2/system/api-keys")
def api_keys():
    try:
        _pwd2 = None
        try:
            with open('/etc/mysql/debian.cnf') as _f:
                _c = _f.read()
            import re as _rr
            _m = _rr.search(r'password\s*=\s*(\S+)', _c)
            if _m:
                _pwd2 = _m.group(1)
        except:
            pass
        if not _pwd2:
            return ok({"keys": [], "error": "无法读取数据库密码"})
        import pymysql as _pm2
        _c2 = _pm2.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=_pwd2, charset="utf8mb4",
                           database='openclaw_config')
        _cu2 = _c2.cursor()
        _cu2.execute("SELECT id, api_key, name, description, is_active FROM api_credentials ORDER BY id")
        _cols2 = [d[0] for d in _cu2.description]
        _keys = [dict(zip(_cols2, r)) for r in _cu2.fetchall()]
        for _k in _keys:
            _ak = str(_k['api_key'])
            if len(_ak) > 12:
                _k['api_key'] = _ak[:6] + '****' + _ak[-4:]
        _cu2.close(); _c2.close()
        return ok({"keys": _keys})
    except Exception as _e:
        return ok({"keys": [], "error": str(_e)})


@app.route("/api/v2/api-keys/<int:key_id>")
def api_key_detail(key_id):
    """获取单个API Key明文（供页面"显示"功能使用）"""
    try:
        _pwd2 = None
        try:
            with open('/etc/mysql/debian.cnf') as _f:
                _c = _f.read()
            import re as _rr
            _m = _rr.search(r'password\s*=\s*(\S+)', _c)
            if _m:
                _pwd2 = _m.group(1)
        except:
            pass
        if not _pwd2:
            return ok({"api_key": None, "error": "无法读取数据库密码"})
        import pymysql as _pm2
        _c2 = _pm2.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=_pwd2, charset="utf8mb4",
                           database='openclaw_config')
        _cu2 = _c2.cursor()
        _cu2.execute("SELECT id, api_key, name, description, is_active FROM api_credentials WHERE id=%s", (key_id,))
        row = _cu2.fetchone()
        _cu2.close(); _c2.close()
        if not row:
            return ok({"api_key": None, "error": "Key不存在"})
        return ok({"api_key": row[1], "name": row[2], "id": row[0], "is_active": bool(row[4])})
    except Exception as _e:
        return ok({"api_key": None, "error": str(_e)})


@app.route("/api/v2/system/cron-status")
def cron_status():
    return ok({"tasks": [], "status": "running"})

@app.route("/api/v2/system/db-tables")
def db_tables():
    c = conn(); cu = c.cursor()
    cu.execute("SELECT TABLE_NAME, TABLE_ROWS, ENGINE, TABLE_COMMENT, CREATE_TIME FROM information_schema.TABLES WHERE TABLE_SCHEMA='stock_db_v2' ORDER BY TABLE_NAME")
    tables = []
    for r in cu.fetchall():
        tables.append({'name': r[0], 'rows': r[1] or 0, 'engine': r[2] or '', 'comment': r[3] or ''})
    
    # 获取每张表的列信息
    columns = {}
    cu.execute("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, COLUMN_COMMENT, EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='stock_db_v2' ORDER BY TABLE_NAME, ORDINAL_POSITION")
    for r in cu.fetchall():
        tbl = r[0]
        if tbl not in columns:
            columns[tbl] = []
        columns[tbl].append({
            'name': r[1], 'type': r[2], 'nullable': r[3],
            'key': r[4] or '', 'default': r[5] if r[5] else '',
            'comment': r[6] if r[6] else '', 'extra': r[7] or ''
        })
    
    # 索引信息
    indexes = {}
    cu.execute("SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE, INDEX_TYPE FROM information_schema.STATISTICS WHERE TABLE_SCHEMA='stock_db_v2' ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX")
    for r in cu.fetchall():
        tbl = r[0]; idx = r[1]
        if tbl not in indexes:
            indexes[tbl] = {}
        if idx not in indexes[tbl]:
            indexes[tbl][idx] = {'columns': [], 'unique': not r[4], 'type': r[5] or ''}
        indexes[tbl][idx]['columns'].append(r[2])
    
    cu.close(); c.close()
    return ok({"tables": tables, "columns": columns, "indexes": indexes, "total": len(tables)})



@app.route("/api/v2/watch-pool/list")
def watch_pool_list():
    c = conn(); cu = c.cursor()
    keyword = request.args.get('keyword', '')
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 200))
    offset = (page - 1) * page_size
    
    if keyword:
        kw = '%' + keyword + '%'
        cu.execute("SELECT COUNT(*) FROM watch_pool WHERE 1=1 AND (ts_code LIKE %s OR name LIKE %s)", (kw, kw))
        total = cu.fetchone()[0]
        cu.execute("SELECT ts_code, name FROM watch_pool WHERE 1=1 AND (ts_code LIKE %s OR name LIKE %s) ORDER BY name LIMIT %s OFFSET %s", (kw, kw, page_size, offset))
    else:
        cu.execute("SELECT COUNT(*) FROM watch_pool WHERE 1=1")
        total = cu.fetchone()[0]
        cu.execute("SELECT ts_code, name FROM watch_pool WHERE 1=1 ORDER BY name LIMIT %s OFFSET %s", (page_size, offset))
    
    stocks = [{'ts_code': r[0], 'name': r[1] or ''} for r in cu.fetchall()]
    cu.close(); c.close()
    return ok({'stocks': stocks, 'total': total})

@app.route("/api/v2/watch-pool/score-list")
def watch_pool_score():
    c = conn(); cu = c.cursor()
    keyword = request.args.get('keyword', '')
    cu.execute("SELECT MAX(trade_date) FROM strategy_signal")
    td = str(cu.fetchone()[0])
    
    params = [td, td]
    sql = """
        SELECT wp.ts_code, COALESCE(wp.name, sb.name, ''), ss.composite_score, ss.calibrated_score, ss.raw_score,
               ss.trend_score, ss.momentum_score, ss.structure_score, ss.emotion_score,
               ss.signal_label, ss.direction, ss.season, ss.pos_score, ss.mf_score, ss.margin_score,
               ss.vol_ratio, COALESCE(sb.industry, ''),
               dk.close, dk.change_pct
        FROM watch_pool wp
        LEFT JOIN stock_basic sb ON wp.ts_code = sb.ts_code
        LEFT JOIN strategy_signal ss ON wp.ts_code = ss.ts_code AND ss.trade_date = %s
        LEFT JOIN daily_kline_qfq dk ON wp.ts_code = dk.ts_code AND dk.trade_date = %s
        WHERE wp.is_active=1
    """
    
    if keyword:
        kw = '%%' + keyword + '%%'
        sql += " AND (wp.ts_code LIKE %s OR COALESCE(wp.name, sb.name, '') LIKE %s)"
        params += [kw, kw]
    
    sql += " ORDER BY ss.calibrated_score DESC, ss.composite_score DESC, wp.name ASC"
    cu.execute(sql, params)
    stocks = []
    for r in cu.fetchall():
        stocks.append({
            'ts_code': r[0],
            'name': r[1] or '',
            'composite_score': float(r[2]) if r[2] else None,
            'calibrated_score': float(r[3]) if r[3] else None,
            'raw_score': float(r[4]) if r[4] else None,
            'trend_score': float(r[5]) if r[5] else None,
            'momentum_score': float(r[6]) if r[6] else None,
            'structure_score': float(r[7]) if r[7] else None,
            'emotion_score': float(r[8]) if r[8] else None,
            'signal_label': r[9] or '',
            'direction': r[10] or '',
            'season': r[11] or '',
            'pos_score': float(r[12]) if len(r) > 12 and r[12] else None,
            'mf_score': float(r[13]) if len(r) > 13 and r[13] else None,
            'margin_score': float(r[14]) if len(r) > 14 and r[14] else None,
            'vol_ratio': float(r[15]) if len(r) > 15 and r[15] else None,
            'industry': r[16] if len(r) > 16 else '',
            'close_price': float(r[17]) if len(r) > 17 and r[17] else None,
            'change_pct': float(r[18]) if len(r) > 18 and r[18] else None,
        })
    cu.close(); c.close()
    return ok({'stocks': stocks, 'total': len(stocks), 'trade_date': td})

@app.route("/api/v2/watch-pool/add", methods=['POST'])
def watch_pool_add():
    d = request.get_json() or {}
    ts_code = d.get('ts_code', '')
    name = d.get('name', '')
    if not ts_code:
        return err('缺少ts_code')
    c = conn(); cu = c.cursor()
    try:
        cu.execute("INSERT IGNORE INTO watch_pool (ts_code, name, 1=1) VALUES (%s, %s, 1)", (ts_code, name))
        c.commit()
        cu.close(); c.close()
        return ok({'ts_code': ts_code})
    except Exception as e:
        return err(str(e))

@app.route("/api/v2/watch-pool/batch-add", methods=['POST'])
def watch_pool_batch():
    d = request.get_json() or {}
    codes = d.get('codes', [])
    added = 0
    c = conn(); cu = c.cursor()
    for item in codes:
        code = item.get('ts_code', '') if isinstance(item, dict) else item
        nm = item.get('name', '') if isinstance(item, dict) else ''
        try:
            cu.execute("INSERT IGNORE INTO watch_pool (ts_code, name, 1=1) VALUES (%s, %s, 1)", (code, nm))
            added += 1
        except: pass
    c.commit(); cu.close(); c.close()
    return ok({'added': added})

@app.route("/api/v2/backtest-pool/list")
def backtest_pool_list():
    c = conn(); cu = c.cursor()
    keyword = request.args.get('keyword', '')
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 200))
    offset = (page - 1) * page_size
    
    if keyword:
        kw = '%' + keyword + '%'
        cu.execute("SELECT COUNT(*) FROM backtest_pool WHERE (ts_code LIKE %s OR name LIKE %s)", (kw, kw))
        total = cu.fetchone()[0]
        cu.execute("SELECT ts_code, name, industry, market, 1=1 FROM backtest_pool WHERE (ts_code LIKE %s OR name LIKE %s) ORDER BY ts_code LIMIT " + str(page_size) + " OFFSET " + str(offset), (kw, kw))
    else:
        cu.execute("SELECT COUNT(*) FROM backtest_pool")
        total = cu.fetchone()[0]
        cu.execute("SELECT ts_code, name, industry, market, 1=1 FROM backtest_pool ORDER BY ts_code LIMIT " + str(page_size) + " OFFSET " + str(offset))
    
    stocks = []
    for r in cu.fetchall():
        stocks.append({'ts_code': r[0], 'name': r[1] or '', 'industry': r[2] or '', 'market': r[3] or '', '1=1': r[4]})
    cu.close(); c.close()
    return ok({'stocks': stocks, 'total': total})

@app.route("/api/v2/backtest-pool/add", methods=['POST'])
def backtest_pool_add():
    d = request.get_json() or {}
    ts_code = d.get('ts_code', '').strip()
    name = d.get('name', '').strip()
    if not ts_code:
        return err('缺少ts_code')
    c = conn(); cu = c.cursor()
    try:
        cu.execute("INSERT IGNORE INTO backtest_pool (ts_code, name, 1=1) VALUES (%s, %s, 1)", (ts_code, name))
        c.commit()
        cu.close(); c.close()
        return ok({'ts_code': ts_code})
    except Exception as e:
        cu.close(); c.close()
        return err(str(e))

@app.route("/api/v2/backtest-pool/remove", methods=['POST'])
def backtest_pool_remove():
    d = request.get_json() or {}
    ts_code = d.get('ts_code', '')
    if not ts_code:
        return err('缺少ts_code')
    c = conn(); cu = c.cursor()
    cu.execute("DELETE FROM backtest_pool WHERE ts_code=%s", (ts_code,))
    c.commit()
    cu.close(); c.close()
    return ok({'ts_code': ts_code, 'deleted': True})

@app.route("/api/v2/backtest-pool/batch-add", methods=['POST'])
def backtest_pool_batch():
    d = request.get_json() or {}
    codes = d.get('codes', [])
    added = 0
    c = conn(); cu = c.cursor()
    for item in codes:
        code = item.get('ts_code', '') if isinstance(item, dict) else item
        nm = item.get('name', '') if isinstance(item, dict) else ''
        try:
            cu.execute("INSERT IGNORE INTO backtest_pool (ts_code, name, 1=1) VALUES (%s, %s, 1)", (code, nm))
            added += 1
        except: pass
    c.commit(); cu.close(); c.close()
    return ok({'added': added})

@app.route("/api/v2/backtest-pool/update", methods=['POST'])
def backtest_pool_update():
    d = request.get_json() or {}
    ts_code = d.get('ts_code', '')
    name = d.get('name')
    industry = d.get('industry')
    if not ts_code:
        return err('缺少ts_code')
    c = conn(); cu = c.cursor()
    updates = []
    params = []
    if name is not None:
        updates.append("name=%s"); params.append(name)
    if industry is not None:
        updates.append("industry=%s"); params.append(industry)
    if updates:
        sql = "UPDATE backtest_pool SET " + ",".join(updates) + " WHERE ts_code=%s"
        params.append(ts_code)
        cu.execute(sql, params)
        c.commit()
    cu.close(); c.close()
    return ok({'ts_code': ts_code})




# 注册数据刷新路由
import importlib
_data_refresh = importlib.import_module('routes.data_refresh')
_data_refresh_router = _data_refresh.router
app.register_blueprint(_data_refresh_router)



# ═══════════════════════════════════════════
# 策略版本管理API
# ═══════════════════════════════════════════

@app.route("/api/v2/strategy/versions")
def strategy_versions():
    """获取所有版本列表"""
    c = conn(); cu = c.cursor()
    cu.execute("""
        SELECT version, MIN(version_name) as version_name, MIN(change_desc) as change_desc,
               MIN(created_at) as created_at, COUNT(*) as config_count
        FROM strategy_config_versions
        GROUP BY version
        ORDER BY version DESC
    """)
    cols = [d[0] for d in cu.description]
    rows = [dict(zip(cols, r)) for r in cu.fetchall()]
    cu.close(); c.close()
    return ok({"versions": rows, "current_version": 1})

@app.route("/api/v2/strategy/version/<int:ver>")
def strategy_version_detail(ver):
    """获取指定版本的完整配置详情"""
    c = conn(); cu = c.cursor()
    cu.execute("SELECT config_id, snapshot, version_name, change_desc, created_at FROM strategy_config_versions WHERE version=%s", (ver,))
    rows = cu.fetchall()
    if not rows:
        cu.close(); c.close()
        return err("版本不存在")
    
    import json as _j
    configs = []
    for r in rows:
        snapshot = _j.loads(r[1]) if isinstance(r[1], str) else r[1]
        configs.append(snapshot)
    cu.close(); c.close()
    return ok({"version": ver, "configs": configs, "total": len(configs)})

@app.route("/api/v2/strategy/version/<int:ver>/apply", methods=['POST'])
def strategy_version_apply(ver):
    """将指定版本的配置恢复到strategy_config（切换版本）"""
    c = conn(); cu = c.cursor()
    cu.execute("SELECT config_id, snapshot FROM strategy_config_versions WHERE version=%s", (ver,))
    rows = cu.fetchall()
    if not rows:
        cu.close(); c.close()
        return err("版本不存在")
    
    import json as _j
    restored = 0
    for r in rows:
        snap = _j.loads(r[1]) if isinstance(r[1], str) else r[1]
        cu.execute("""
            UPDATE strategy_config SET
                buy_min_score=%s, p1_score=%s, p2_score=%s, p3_score=%s,
                stop_loss_pct=%s, max_hold_days=%s, cool_days=%s,
                trailing_stop_pct=%s, max_pos_pct=%s,
                description=%s, name=%s
            WHERE id=%s
        """, (snap['buy_min_score'], snap.get('p1', 40), snap.get('p2', 30), snap.get('p3', 20),
              snap['stop_loss'], snap['max_hold'], snap['cool_days'],
              snap['trailing_stop'], snap['max_pos_pct'],
              snap.get('description', ''), snap.get('name', ''),
              snap['id']))
        restored += 1
    c.commit()
    cu.close(); c.close()
    return ok({"version": ver, "restored": restored})

@app.route("/api/v2/strategy/version/snapshot", methods=['POST'])
def strategy_version_snapshot():
    """创建当前配置的快照（新版本）"""
    import json as _j
    d = request.get_json() or {}
    desc = d.get('description', '')
    
    c = conn(); cu = c.cursor()
    
    # 获取当前最大版本号
    cu.execute("SELECT COALESCE(MAX(version), 0) FROM strategy_config_versions")
    max_ver = cu.fetchone()[0]
    new_ver = max_ver + 1
    
    # 读取当前所有策略配置
    cu.execute("SELECT id, name, season_type, buy_min_score, p1_score, p2_score, p3_score, stop_loss_pct, max_hold_days, cool_days, trailing_stop_pct, max_pos_pct, is_active, description, max_total_pct, position_tolerance FROM strategy_config WHERE 1=1 ORDER BY id")
    saved = 0
    for r in cu.fetchall():
        snapshot = _j.dumps({
            'id': r[0], 'name': r[1], 'season_type': r[2], 'buy_min_score': r[3],
            'p1': r[4], 'p2': r[5], 'p3': r[6], 'stop_loss': float(r[7]),
            'max_hold': r[8], 'cool_days': r[9], 'trailing_stop': float(r[10]),
            'max_pos_pct': r[11], 'max_total_pct': r[14] if len(r) > 14 else 30,
            'is_active': bool(r[12]), 'description': r[13]
        }, ensure_ascii=False)
        ver_name = f'V{new_ver}-{r[2]}'
        cu.execute("INSERT INTO strategy_config_versions (version, version_name, config_id, snapshot, change_desc) VALUES (%s, %s, %s, %s, %s)",
                   (new_ver, ver_name, r[0], snapshot, desc))
        saved += 1
    c.commit()
    cu.close(); c.close()
    return ok({"version": new_ver, "saved": saved})
@app.route('/api/v2/market-status')
def api_market_status():
    """市场状态 + 操作建议"""
    cu = conn(); c = cu.cursor()
    c.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date <= CURDATE()")
    td = c.fetchone()[0] or date.today().strftime('%Y-%m-%d')
    
    c.execute("SELECT season, regime, hengjiyuan_level, raw_score FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
    sr = c.fetchone()
    season = sr[0] if sr else 'chaos'
    regime = sr[1] if sr else 'range'
    hengji = sr[2] if sr else 'weak_heng'
    
    # 获取当日评分分布
    c.execute("SELECT COUNT(*) as total, AVG(composite_score) as avg_sc, AVG(calibrated_score) as avg_cal FROM strategy_signal WHERE trade_date=%s", (td,))
    sr2 = c.fetchone()
    c.execute("SELECT COUNT(*) FROM strategy_signal WHERE trade_date=%s AND composite_score>=75", (td,))
    above75 = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM strategy_signal WHERE trade_date=%s AND composite_score>=60 AND composite_score<75", (td,))
    above60 = c.fetchone()[0]
    
    cu.close()
    return ok({
        'trade_date': td,
        'season': season,
        'regime': regime,
        'hengji': hengji,
        'scoring_strategy': 'momentum_v2',
        'hs300_trend': 0,
        'total_stocks': sr2[0] if sr2 else 0,
        'avg_score': round(float(sr2[1] or 0), 1) if sr2 else 0,
        'avg_calibrated': round(float(sr2[2] or 0), 1) if sr2 else 0,
        'above75': above75,
        'above60': above60,
    })


@app.route('/api/v2/board-seasons')
def api_board_seasons():
    """各指数板块季节"""
    c = conn(); cu = c.cursor()
    cu.execute("""SELECT index_code, season FROM season_state 
        WHERE trade_date = (SELECT MAX(trade_date) FROM season_state WHERE index_code!='MARKET')
        AND index_code!='MARKET' AND index_code!='399106.SZ'""")
    seasons_399106 = {'399106.SZ': 'chaos'}
    cu2 = conn(); c2 = cu2.cursor()
    cu2.execute("SELECT season FROM season_state WHERE index_code='399106.SZ' ORDER BY trade_date DESC LIMIT 1")
    r2 = cu2.fetchone()
    if r2:
        seasons_399106['399106.SZ'] = r2[0]
    cu2.close(); c2.close()
    seasons.update(seasons_399106)
    seasons = {r[0]: r[1] for r in cu.fetchall()}
    cu.close(); c.close()
    return ok(seasons)


@app.route('/api/v2/dragon/list')
def api_dragon_list():
    import math
    try:
        c=conn();cu=c.cursor()
        cu.execute("SELECT MAX(trade_date) FROM limit_up_daily")
        lt=cu.fetchone()[0]
        if not lt:return ok({'data':[],'trade_date':'','total':0,'strong':0,'buy':0,'watch':0})
        cu.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date>=%s",(lt,))
        st=cu.fetchone()[0]or lt;td=str(lt);std=str(st)
        cu.execute("SELECT l.ts_code,l.name,l.trade_date,l.limit_up_time,l.open_times,l.sealed,l.change_pct,l.turnover_rate,l.reason FROM limit_up_daily l WHERE l.trade_date=%s AND l.name NOT LIKE CONCAT(CHAR(37),'ST',CHAR(37)) AND l.name NOT LIKE CONCAT(CHAR(37),'*ST',CHAR(37)) ORDER BY l.limit_up_time",(td,))
        rows=[dict(zip([d[0]for d in cu.description],r))for r in cu.fetchall()]
        cu.execute("SELECT ts_code,l_buy,l_sell,net_buy FROM dragon_tiger_daily WHERE trade_date=%s ORDER BY ABS(net_buy)DESC",(td,))
        drm={r[0]:{'l_buy':float(r[1]or 0),'l_sell':float(r[2]or 0),'net_buy':float(r[3]or 0)}for r in cu.fetchall()}
        cu.execute("SELECT index_code,season FROM season_state WHERE trade_date=(SELECT MAX(trade_date)FROM season_state WHERE index_code!='MARKET')AND index_code!='MARKET'")
        isea={r[0]:r[1]for r in cu.fetchall()};cu.close();c.close()
        res=[]
        for r in rows:
            c2=conn().cursor();c2.execute("SELECT trend_score,momentum_score,pos_score,mf_score,margin_score,structure_score,calibrated_score,season FROM strategy_signal WHERE ts_code=%s AND trade_date=%s ORDER BY trade_date DESC LIMIT 1",(r['ts_code'],std));s=c2.fetchone();c2.close()
            tr=float(s[0])if s and s[0]else 50;mo=float(s[1])if s and s[1]else 50;po=float(s[2])if s and s[2]else 50;mf=float(s[3])if s and s[3]else 50;mg=float(s[4])if s and s[4]else 50;stv=float(s[5])if s and s[5]else 50;ca=float(s[6])if s and s[6]else 0
            ds=mo*0.35+tr*0.25+mf*0.20+po*0.10+stv*0.05+mg*0.05
            # 无策略评分时默认加分: 涨停当天+15, 早盘封板(9:25-10:00)+10
            if ca==0:
                ds += 15  # 涨停基准加分
                lt = str(r.get('limit_up_time','') or '')
                if lt and lt >= '09:25' and lt <= '10:00':
                    ds += 10  # 早盘封板加分
            # 打板板块映射（同步市值分档逻辑，与p6_dual_track_engine.MarketContext._get_index_for_code保持一致）
            tc=r['ts_code']
            if tc[:3] in ('688','689'): ix='000688.SH'
            elif tc.startswith('30'): ix='399006.SZ'
            elif tc.endswith('.SH'): ix='000001.SH'  # 沪主板默认上证综指
            elif tc.endswith('.SZ'): ix='399106.SZ'  # 深主板默认深证综指
            else: ix='000300.SH'
            bs=isea.get(ix,'chaos');ds+=5 if'summer'in bs else(-5 if'winter'in bs else 0)
            nb=drm.get(r['ts_code'],{}).get('net_buy',0);ds+=8 if nb>50000000 else(3 if nb>10000000 else(-8 if nb<-50000000 else 0))
            ds=max(0,min(100,round(ds,1)))
            al='strong'if ds>=80 else('buy'if ds>=70 else('watch'if ds>=60 else'pass'))
            lab='🔥 关注'if ds>=80 else('✅ 可打'if ds>=70 else('👀 观察'if ds>=60 else'❌ 放弃'))
            res.append({'ts_code':r['ts_code'],'name':r.get('name',''),'trade_date':td,'limit_time':str(r.get('limit_up_time')or '')[:5],'change_pct':float(r.get('change_pct')or 0),'reason':r.get('reason','')or'','dragon_score':ds,'action':lab,'action_level':al,'trend':round(tr,1),'momentum':round(mo,1),'pos_score':round(po,1),'mf_score':round(mf,1),'margin_score':round(mg,1),'net_buy':round(nb,0),'board_season':bs})
        res.sort(key=lambda x:x['dragon_score'],reverse=True)
        return ok({'data':res,'trade_date':td,'total':len(res),'strong':len([x for x in res if x['action_level']=='strong']),'buy':len([x for x in res if x['action_level']=='buy']),'watch':len([x for x in res if x['action_level']=='watch'])})
    except Exception as e:
        import traceback;return err(str(e)+'|'+traceback.format_exc()[:200])
@app.route('/api/v2/refresh-score', methods=['POST'])
def api_refresh_score():
    """一键刷新V2新引擎评分"""
    """一键刷新V2新引擎评分"""
    try:
        import subprocess
        # 后台启动评分脚本
        subprocess.Popen(['python3', '/tmp/score_v2_insert.py'], 
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ok({'message': '评分已启动，约1分钟后完成'})
    except Exception as e:
        return err(str(e))


@app.route('/api/v2/short-term/evaluate', methods=['GET'])
def api_short_term_evaluate():
    """短期过滤器评估（单股/全持仓）"""
    ts_code = request.args.get('ts_code')
    try:
        from short_term_filter import evaluate, evaluate_holdings
        if ts_code:
            # 查买入日期
            c = conn(); cu = c.cursor()
            cu.execute("SELECT buy_date FROM portfolio_holdings WHERE ts_code=%s AND (status='HOLDING' OR status='hold')", (ts_code,))
            r = cu.fetchone()
            cu.close(); c.close()
            if not r:
                return err('未找到持仓记录')
            buy_date = str(r[0]) if r[0] else None
            result = evaluate(ts_code, buy_date, hold_days=5)
            return ok({'result': result})
        else:
            results = evaluate_holdings()
            return ok({'results': results})
    except Exception as e:
        import traceback
        return err(str(e)[:200])


@app.route('/api/v2/system/backup', methods=['POST'])
def api_backup():
    """备份stock_db_v2数据库"""
    try:
        import subprocess, os, glob
        from datetime import datetime
        now = datetime.now().strftime('%Y%m%d_%H%M')
        backup_dir = '/root/backup'
        os.makedirs(backup_dir, exist_ok=True)
        # 清除旧备份
        for f in glob.glob(backup_dir + '/stock_db_v2_*.sql.gz'):
            os.remove(f)
        # 执行备份
        cmd = "mysqldump -u debian-sys-maint -p'iXve1rVBXfdA4tL9' --databases stock_db_v2 --routines --triggers --single-transaction --quick 2>/dev/null | gzip > " + backup_dir + "/stock_db_v2_" + now + ".sql.gz"
        ret = subprocess.run(cmd, shell=True, capture_output=True, timeout=300)
        if ret.returncode != 0:
            return err('备份失败')
        fs = os.path.getsize(backup_dir + '/stock_db_v2_' + now + '.sql.gz')
        return ok({'path': backup_dir + '/stock_db_v2_' + now + '.sql.gz', 'size_mb': round(fs/1024/1024, 1), 'time': now})
    except Exception as e:
        return err(str(e)[:100])


@app.route('/api/v2/trade/records', methods=['GET'])
def api_trade_records():
    """获取交易记录"""
    ts_code = request.args.get('ts_code')
    limit = request.args.get('limit', 100)
    c = conn(); cu = c.cursor()
    if ts_code:
        cu.execute("SELECT * FROM trade_records WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s", (ts_code, int(limit)))
    else:
        cu.execute("SELECT * FROM trade_records ORDER BY trade_date DESC LIMIT %s", (int(limit),))
    cols = [d[0] for d in cu.description]
    rows = [dict(zip(cols, r)) for r in cu.fetchall()]
    for r in rows:
        r['price'] = float(r['price'])
        r['amount'] = float(r['amount'])
        r['commission'] = float(r.get('commission',0) or 0)
        r['qty'] = int(r['qty'])
    cu.close(); c.close()
    return ok({'records': rows, 'total': len(rows)})


@app.route('/api/v2/trade/records', methods=['POST'])
def api_trade_add():
    """新增交易记录"""
    d = request.get_json() or {}
    ts_code = d.get('ts_code','')
    name = d.get('name','')
    trade_date = d.get('trade_date','')
    direction = d.get('direction','BUY')
    qty = int(d.get('qty',0))
    price = float(d.get('price',0))
    commission = float(d.get('commission',0) or 0)
    notes = d.get('notes','')[:200]
    amount = round(qty * price, 2)
    
    if not ts_code or not qty or not price:
        return err('缺少必要参数')
    
    c = conn(); cu = c.cursor()
    cu.execute("""
        INSERT INTO trade_records (ts_code, name, trade_date, direction, qty, price, amount, commission, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (ts_code, name, trade_date, direction, qty, price, amount, commission, notes))
    c.commit()
    cu.close(); c.close()
    
    # 同步更新持仓：BUY加仓，SELL减仓
    sync_portfolio(ts_code, name)
    
    return ok({'id': cu.lastrowid})



def sync_portfolio(ts_code, name):
    """根据交易记录同步持仓"""
    c = conn(); cu = c.cursor()
    cu.execute("""
        SELECT direction, SUM(qty) as total_qty, SUM(amount) as total_amount, SUM(commission) as total_comm
        FROM trade_records WHERE ts_code=%s GROUP BY direction
    """, (ts_code,))
    rows = []
    for r in cu.fetchall():
        rows.append({'direction': r[0], 'total_qty': float(r[1] or 0), 'total_amount': float(r[2] or 0), 'total_comm': float(r[3] or 0)})
    buy = next((r for r in rows if r['direction'] == 'BUY'), {'total_qty': 0, 'total_amount': 0, 'total_comm': 0})
    sell = next((r for r in rows if r['direction'] == 'SELL'), {'total_qty': 0, 'total_amount': 0, 'total_comm': 0})
    hold_qty = int(buy['total_qty']) - int(sell['total_qty'])
    if hold_qty > 0:
        buy_cost = buy['total_amount'] + buy['total_comm']
        sell_amount = sell['total_amount']
        cost_price = round((buy_cost - sell_amount) / hold_qty, 4) if hold_qty > 0 else 0
        cu.execute("""
            INSERT INTO portfolio_holdings (ts_code, name, shares, cost_price, status)
            VALUES (%s,%s,%s,%s,'HOLDING')
            ON DUPLICATE KEY UPDATE name=VALUES(name), shares=VALUES(shares), cost_price=VALUES(cost_price), status='HOLDING'
        """, (ts_code, name, hold_qty, cost_price))
    else:
        cu.execute("UPDATE portfolio_holdings SET shares=0, status='CLOSED' WHERE ts_code=%s", (ts_code,))
    c.commit()
    cu.close(); c.close()

@app.route('/api/v2/trade/records/<int:record_id>', methods=['DELETE'])
def api_trade_delete(record_id):
    """删除交易记录"""
    c = conn(); cu = c.cursor()
    cu.execute("SELECT ts_code, name FROM trade_records WHERE id=%s", (record_id,))
    r = cu.fetchone()
    if not r:
        return err('记录不存在')
    cu.execute("DELETE FROM trade_records WHERE id=%s", (record_id,))
    c.commit()
    sync_portfolio(r['ts_code'], r.get('name',''))
    cu.close(); c.close()
    return ok({'deleted': record_id})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8891))
    print("V2统一API :" + str(port) + " DB:stock_db_v2")
    app.run(host='0.0.0.0', port=port, debug=False)
