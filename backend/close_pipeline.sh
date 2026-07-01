#!/bin/bash
# ============================================
# 收盘后全量数据拉取 + 技术指标 + 季节判定 + 评分管道
# 交易日 16:00~17:00 自动执行(由crontab触发)
# ============================================
# set -e 已移除(避免Tushare接口临时无数据导致全管道退出)
# Token 从 db_config.py 的 get_tushare_token() 动态获取,不硬编码
cd /root/stock-system-v2/backend

LOG=/tmp/stock_pipeline_v2.log
echo "[$(date '+%H:%M:%S')] 🚀 收盘管道启动" >> $LOG

# [1/9] 基础数据:daily_basic + moneyflow + margin_detail + 指数日线
# 由 close_pipeline.py 统一执行(margin_detail 自带T+1~T+2自等待逻辑)
echo "[1/9] 拉取 daily_kline + daily_basic + moneyflow + margin_detail + 指数日线..." >> $LOG
python3 close_pipeline.py >> $LOG 2>&1

# [2/9] 技术指标计算
echo "[2/9] 技术指标计算..." >> $LOG
python3 -c "
import sys; sys.path.insert(0,'.')
from db_config import get_connection
import tushare as ts
from db_config import get_tushare_token

ts.set_token(get_tushare_token())
pro = ts.pro_api()
conn = get_connection()
c = conn.cursor()

# 获取监控池所有股票的最新交易日
c.execute('SELECT MAX(trade_date) FROM daily_kline')
td = str(c.fetchone()['MAX(trade_date)'])

# 检查technical_indicator最新日期
c.execute('SELECT MAX(trade_date) FROM technical_indicator')
last_ti = c.fetchone()['MAX(trade_date)']
if str(last_ti) >= td:
    print(f'  技术指标已是最新({td}), 跳过')
else:
    from datetime import datetime
    td_fmt = datetime.strptime(td, '%Y-%m-%d').strftime('%Y%m%d')
    saved = 0
    import signal
    class TimeoutError(Exception): pass
    def handler(signum, frame): raise TimeoutError('stk_factor超时')
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(120)  # 最多等120秒
    try:
        df = pro.stk_factor(trade_date=td_fmt, fields='ts_code,trade_date,ma_5,ma_10,ma_20,ma_60,ma_120,ma_250,rsi_12,macd_dif,macd_dea,atr_14,boll_upper,boll_mid,boll_lower,volume_ratio')
        signal.alarm(0)
        print(f'  拉取{len(df)}条技术指标')
    except TimeoutError:
        print('  ⚠️ stk_factor超时120s, 跳过技术指标')
        saved = -1
    except Exception as e:
        signal.alarm(0)
        print(f'  ⚠️ stk_factor失败: {str(e)[:60]}, 跳过')
        saved = -1
    
    if saved < 0:
        pass  # 已经超时/出错了
    else:
        saved = 0
        for _, r in df.iterrows():
            try:
                c.execute('''INSERT INTO technical_indicator
                (ts_code, trade_date, ma_5, ma_10, ma_20, ma_60, ma_120, ma_250,
                 rsi_12, macd_dif, macd_dea, atr_14, boll_upper, boll_mid, boll_lower,
                 volume_ratio)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE ma_5=VALUES(ma_5), ma_10=VALUES(ma_10),
                    ma_20=VALUES(ma_20), ma_60=VALUES(ma_60), ma_120=VALUES(ma_120),
                    ma_250=VALUES(ma_250), rsi_12=VALUES(rsi_12),
                    macd_dif=VALUES(macd_dif), macd_dea=VALUES(macd_dea),
                    atr_14=VALUES(atr_14), boll_upper=VALUES(boll_upper),
                    boll_mid=VALUES(boll_mid), boll_lower=VALUES(boll_lower),
                    volume_ratio=VALUES(volume_ratio)''',
                (r['ts_code'], td,
                 float(r.get('ma_5',0)or 0), float(r.get('ma_10',0)or 0),
                 float(r.get('ma_20',0)or 0), float(r.get('ma_60',0)or 0),
                 float(r.get('ma_120',0)or 0), float(r.get('ma_250',0)or 0),
                 float(r.get('rsi_12',50)or 50), float(r.get('macd_dif',0)or 0),
                 float(r.get('macd_dea',0)or 0), float(r.get('atr_14',0)or 0),
                 float(r.get('boll_upper',0)or 0), float(r.get('boll_mid',0)or 0),
                 float(r.get('boll_lower',0)or 0),
                 float(r.get('volume_ratio',1)or 1)))
                saved += 1
            except Exception as e:
                pass
    if saved >= 0:
        conn.commit()
        print(f'  入库{saved}条 ✅')
    else:
        print(f'  ⚠️ 技术指标跳过(已超时)')
conn.close()
" >> $LOG 2>&1

# [3/9] 缠论分析
echo "[3/9] 缠论结构分析(完整算法)..." >> $LOG
python3 -c "
import sys
sys.path.insert(0,'.')
from db_config import get_connection
import pymysql, re

v2_conn = get_connection()
v2_c = v2_conn.cursor()

v2_c.execute('SELECT MAX(trade_date) FROM daily_kline')
td = str(v2_c.fetchone()['MAX(trade_date)'])

v2_c.execute('SELECT MAX(trade_date) FROM chanlun_structure')
last_cl = v2_c.fetchone()['MAX(trade_date)']
last_cl_str = str(last_cl) if last_cl else '无数据'
print(f'  K线最新: {td}, 缠论最新: {last_cl_str}')

if last_cl_str >= td:
    print('  缠论已是最新, 跳过')
else:
    v2_c.execute('SELECT ts_code FROM watch_pool')
    codes = [r['ts_code'] for r in v2_c.fetchall()]
    print(f'  开始分析{len(codes)}只股票...')

    from engine.chanlun_analyzer import analyze_chanlun

    done = 0
    for code in codes:
        try:
            v2_c.execute('''SELECT trade_date, open, high, low, close, vol FROM daily_kline
                WHERE ts_code=%s ORDER BY trade_date ASC''', (code,))
            rows = v2_c.fetchall()
            if len(rows) < 60: continue

            ohlc = [{'trade_date':str(r['trade_date']),'open':float(r['open']),'high':float(r['high']),'low':float(r['low']),'close':float(r['close']),'vol':float(r['vol'])} for r in rows]

            result = analyze_chanlun(code, td, ohlc)
            if not result or 'error' in result: continue

            v2_c.execute('''INSERT INTO chanlun_structure
                (ts_code, trade_date, analysis_level,
                 top_fractal_cnt, bottom_fractal_cnt,
                 bi_direction, bi_strength,
                 zhongshu_count, zhongshu_zd, zhongshu_zg, zhongshu_width, zhongshu_stability,
                 zoushi_type, zoushi_stage,
                 beichi_type, beichi_strength, beichi_validity, macd_area_ratio, dif_dea_diverge,
                 buy_sell_point, buy3_confirmed, buy3_failed,
                 autumn_tiger, tiger_confidence, tiger_reasons,
                 structure_score, is_calculable, calc_error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    analysis_level=VALUES(analysis_level),
                    top_fractal_cnt=VALUES(top_fractal_cnt), bottom_fractal_cnt=VALUES(bottom_fractal_cnt),
                    bi_direction=VALUES(bi_direction), bi_strength=VALUES(bi_strength),
                    zhongshu_count=VALUES(zhongshu_count),
                    zhongshu_zd=VALUES(zhongshu_zd), zhongshu_zg=VALUES(zhongshu_zg),
                    zhongshu_width=VALUES(zhongshu_width), zhongshu_stability=VALUES(zhongshu_stability),
                    zoushi_type=VALUES(zoushi_type), zoushi_stage=VALUES(zoushi_stage),
                    beichi_type=VALUES(beichi_type), beichi_strength=VALUES(beichi_strength),
                    beichi_validity=VALUES(beichi_validity), macd_area_ratio=VALUES(macd_area_ratio),
                    dif_dea_diverge=VALUES(dif_dea_diverge),
                    buy_sell_point=VALUES(buy_sell_point), buy3_confirmed=VALUES(buy3_confirmed), buy3_failed=VALUES(buy3_failed),
                    autumn_tiger=VALUES(autumn_tiger), tiger_confidence=VALUES(tiger_confidence), tiger_reasons=VALUES(tiger_reasons),
                    structure_score=VALUES(structure_score), is_calculable=VALUES(is_calculable), calc_error=VALUES(calc_error)''',
                (result['ts_code'], result['trade_date'], result['analysis_level'],
                 result['top_fractal_cnt'], result['bottom_fractal_cnt'],
                 result['bi_direction'], result['bi_strength'],
                 result['zhongshu_count'], result['zhongshu_zd'], result['zhongshu_zg'],
                 result['zhongshu_width'], result['zhongshu_stability'],
                 result['zoushi_type'], result['zoushi_stage'],
                 result['beichi_type'], result['beichi_strength'], result['beichi_validity'],
                 result['macd_area_ratio'], result['dif_dea_diverge'],
                 result['buy_sell_point'], result['buy3_confirmed'], result['buy3_failed'],
                 result['autumn_tiger'], result['tiger_confidence'], result['tiger_reasons'],
                 result['structure_score'], result['is_calculable'], result['calc_error']))
            done += 1
        except Exception as e:
            pass
    v2_conn.commit()
    print(f'  缠论分析完成: {done}/{len(codes)}只 ✅')
v2_conn.close()
" >> $LOG 2>&1

# [4/9] 季节判定
echo "[4/9] 季节判定..." >> $LOG
python3 -c "
import sys; sys.path.insert(0,'.')
from season_engine import SeasonEngine, save_result_to_db
e = SeasonEngine()
r = e.judge_market_season()
save_result_to_db(r)
print(f'  结果: {r.get(\"market_season\")}/{r.get(\"market_regime\")} 评分:{r.get(\"raw_score\",0):.2f}')
" >> $LOG 2>&1

# [5/9] P6双轨评分 + 评分快照
echo "[5/9] P6双轨评分..." >> $LOG
python3 score_pipeline.py >> $LOG 2>&1

# 保存当日评分快照
echo "  保存评分快照..." >> $LOG
python3 << 'PYEOF' >> $LOG 2>&1
import sys; sys.path.insert(0, '.')
from db_config import get_connection
conn = get_connection()
c = conn.cursor()

c.execute("SELECT MAX(trade_date) as d FROM strategy_signal")
td = str(c.fetchone()['d'])
print(f'  快照日期: {td}', flush=True)

c.execute("""SELECT ss.ts_code, ss.calibrated_score, ss.composite_score, ss.season,
    COALESCE(sb.name, '') as name, COALESCE(sb.industry, '') as industry,
    dk.close, dk.change_pct
FROM strategy_signal ss
LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
LEFT JOIN daily_kline dk ON ss.ts_code = dk.ts_code AND dk.trade_date = %s
WHERE ss.trade_date = %s""", (td, td))

rows = c.fetchall()
saved = 0
for r in rows:
    try:
        c.execute("SELECT signal_label, buy_sell_ratio FROM intraday_signals WHERE ts_code=%s AND trade_date=%s", (r['ts_code'], td))
        sig_row = c.fetchone()
        sig_label = sig_row['signal_label'] if sig_row and sig_row.get('signal_label') else ''
        sig_ratio = float(sig_row['buy_sell_ratio'] or 0) if sig_row else 0

        c.execute("""INSERT INTO daily_score_snapshot
            (trade_date, ts_code, calibrated_score, composite_score, name, industry, close_price, change_pct, season, signal_label, buy_sell_ratio)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
            calibrated_score=VALUES(calibrated_score), composite_score=VALUES(composite_score),
            name=VALUES(name), industry=VALUES(industry),
            close_price=VALUES(close_price), change_pct=VALUES(change_pct),
            season=VALUES(season),
            signal_label=VALUES(signal_label), buy_sell_ratio=VALUES(buy_sell_ratio)""",
            (td, r['ts_code'], float(r['calibrated_score'] or 0), float(r['composite_score'] or 0),
             r['name'] or '', r['industry'] or '',
             float(r['close'] or 0), float(r['change_pct'] or 0),
             r['season'] or '', sig_label, sig_ratio))
        saved += 1
        if saved % 100 == 0: conn.commit()
    except: pass
conn.commit()
print(f'  快照入库: {saved}条 ✅', flush=True)
c.close(); conn.close()
PYEOF

# [6/9] 打板(涨停)数据刷新
printf '  [6/9] 打板数据刷新...\n' >> $LOG
cd "$(dirname "$0")"
python3 scripts/refresh_dragon_data.py >> $LOG 2>&1

# [7/9] 打板评分快照 - 从dragon API取当日涨停股完整评分写入快照表
printf '  [7/9] 打板评分快照...\n' >> $LOG
cd "$(dirname "$0")"
python3 -c "
import sys, json, requests
sys.path.insert(0, '.')
from db_config import get_connection

try:
    r = requests.get('http://localhost:8891/api/v2/dragon/list', headers={'X-API-Key': '90a275cbcc004fd5'}, timeout=30)
    data = r.json().get('data', {})
    rows = data.get('data', [])
    td = data.get('trade_date', '')

    if not rows:
        print(f'  打板快照: 无涨停数据(交易日{td})', flush=True)
    else:
        conn = get_connection()
        c = conn.cursor()
        saved = 0
        for s in rows:
            c.execute('''INSERT INTO dragon_snapshot
                (trade_date, ts_code, name, change_pct, limit_up_time, reason,
                 dragon_score, action_level, action,
                 momentum, mf_score, trend, pos_score, margin_score,
                 net_buy, board_season)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                name=VALUES(name), change_pct=VALUES(change_pct),
                limit_up_time=VALUES(limit_up_time), reason=VALUES(reason),
                dragon_score=VALUES(dragon_score), action_level=VALUES(action_level),
                action=VALUES(action), momentum=VALUES(momentum),
                mf_score=VALUES(mf_score), trend=VALUES(trend),
                pos_score=VALUES(pos_score), margin_score=VALUES(margin_score),
                net_buy=VALUES(net_buy), board_season=VALUES(board_season)''',
                (td, s['ts_code'], s.get('name',''), s.get('change_pct',0),
                 s.get('limit_time',''), s.get('reason',''),
                 s.get('dragon_score',0), s.get('action_level',''), s.get('action',''),
                 s.get('momentum',0), s.get('mf_score',0), s.get('trend',0),
                 s.get('pos_score',0), s.get('margin_score',0),
                 s.get('net_buy',0), s.get('board_season','')))
            saved += 1
            if saved % 50 == 0: conn.commit()
        conn.commit()
        c.close()
        conn.close()
        print(f'  打板快照: {saved}条 ({td})', flush=True)
except Exception as e:
    print(f'  ❌ 打板快照失败: {e}', flush=True)
" >> $LOG 2>&1

# [8/9] 板块数据聚合
printf '  [8/9] 板块数据聚合...\n' >> $LOG
python3 << 'PYEOF2' >> $LOG 2>&1
import sys; sys.path.insert(0, '.')
from db_config import get_connection
conn = get_connection()
c = conn.cursor()

c.execute("SELECT MAX(trade_date) as d FROM daily_kline")
td = str(c.fetchone()['d'])

# 取该日所有个股涨跌幅 + 行业
c.execute("""
    SELECT sb.industry, AVG(dk.change_pct) as avg_pct,
           SUM(dk.vol) as tot_vol, SUM(dk.amount) as tot_amt
    FROM daily_kline dk
    JOIN stock_basic sb ON dk.ts_code = sb.ts_code
    WHERE dk.trade_date = %s
      AND sb.industry IS NOT NULL AND sb.industry != ''
      AND sb.is_active = 1
    GROUP BY sb.industry
""", (td,))

rows = c.fetchall()
saved = 0
for r in rows:
    ind = r['industry'].strip()[:30]
    if not ind: continue
    avg_pct = float(r['avg_pct'] or 0)
    vol = float(r['tot_vol'] or 0)
    amt = float(r['tot_amt'] or 0)
    c.execute("""INSERT INTO sector_index_daily
        (sector_code, trade_date, open, close, high, low, change_pct, vol, amount)
        VALUES (%s,%s,100,100,100,100,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
        change_pct=VALUES(change_pct), vol=VALUES(vol), amount=VALUES(amount)""",
        (ind, td, round(avg_pct,2), round(vol), round(amt)))
    saved += 1
    if saved % 50 == 0: conn.commit()
conn.commit()
print(f'  板块聚合完成: {saved}个行业 ✅', flush=True)
c.close(); conn.close()
PYEOF2

# [9/9] 刷新持仓现价
printf '  [9/9] 刷新持仓现价...\n' >> $LOG
python3 << 'PYEOF3' >> $LOG 2>&1
import sys; sys.path.insert(0, '.')
from db_config import get_connection
conn = get_connection()
c = conn.cursor()

c.execute("SELECT MAX(trade_date) FROM daily_kline")
td = str(c.fetchone()['MAX(trade_date)'])

c.execute("""SELECT p.id, p.ts_code, p.name, p.cost_price, p.shares FROM portfolio_holdings p
    WHERE p.status='HOLDING' AND p.shares > 0""")
holdings = c.fetchall()
print(f'  刷新{len(holdings)}只持仓现价(最新交易日: {td})', flush=True)

updated = 0
for h in holdings:
    c.execute("""SELECT `close` FROM daily_kline_qfq
        WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1""", (h['ts_code'], td))
    r = c.fetchone()
    if not r:
        continue
    close = float(r['close'])
    shares = float(h['shares'])
    cost_price = float(h['cost_price'])
    c.execute("""UPDATE portfolio_holdings SET
        current_price=%s,
        market_value=ROUND(%s * %s, 2),
        profit_amount=ROUND(%s * %s - %s * %s, 2),
        profit_pct=ROUND((%s - %s) / %s * 100, 3),
        updated_at=NOW()
        WHERE id=%s""",
        (close, close, shares, close, shares, cost_price, shares, close, cost_price, cost_price, h['id']))
    updated += 1

conn.commit()
c.close(); conn.close()
print(f'  持仓现价刷新完成: {updated}只 ✅', flush=True)
PYEOF3

echo "[$(date '+%H:%M:%S')] ✅ 收盘管道完成" >> $LOG
