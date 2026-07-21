#!/bin/bash
# 逐日重跑缺失的评分数据，每天跑完退出Python释放内存
set -e

cd /root/stock-system-v2/backend
LOG=/tmp/rebatch_tail.log

DATES=(
  2026-06-23
  2026-06-24
  2026-06-25
  2026-07-03
  2026-07-07
  2026-07-08
  2026-07-09
  2026-07-10
  2026-07-13
  2026-07-15
)

for TD in "${DATES[@]}"; do
    echo "[$(date '+%H:%M:%S')] 开始跑 $TD ..." | tee -a "$LOG"
    
    # 调用单日跑分
    SKIP_REALTIME=1 python3 -c "
import sys, os, json, time
sys.path.insert(0, '.')
os.environ['SKIP_REALTIME'] = '1'

from p6_dual_track_engine import batch_score, MarketContext
from db_config import get_connection

conn = get_connection()
cur = conn.cursor()
cur.execute(\"\"\"
    SELECT season, regime, confidence, raw_score, hengjiyuan_level,
           hengjiyuan_score, chaos_subtype, regime_strength, scoring_strategy
    FROM season_state
    WHERE index_code='MARKET' AND trade_date='$TD'
\"\"\")
row = cur.fetchone()
if not row:
    print('❌ 无季节数据')
    sys.exit(1)

judge = {
    'market_season': row['season'],
    'market_regime': row['regime'],
    'market_confidence': float(row['confidence'] or 0.5),
    'market_scoring_strategy': row.get('scoring_strategy') or 'momentum',
    'trade_date': '$TD',
    'market_raw_score': float(row['raw_score'] or 50),
    'hengjiyuan_level': row['hengjiyuan_level'] or 'weak_heng',
    'hengjiyuan_score': float(row['hengjiyuan_score'] or 50),
    'chaos_subtype': row['chaos_subtype'],
    'regime_strength': float(row['regime_strength'] or 1.0),
}
ctx = MarketContext(judge)

cur.execute('SELECT ts_code FROM watch_pool WHERE is_active=1')
codes = [r['ts_code'] for r in cur.fetchall()]
cur.close()

t0 = time.time()
results = batch_score(codes, ctx)
elapsed = time.time() - t0

# 写入DB
cur = conn.cursor()
saved = 0
for r in results:
    code = r['ts_code']
    comp_score = float(r.get('score', 0) or 0)
    calib_score = float(r.get('calibrated_score', comp_score) or 0)
    track = r.get('track', '')
    stf = r.get('stf', {})
    det = r.get('details', {}) or {}
    p_score = float(det.get('penalty_score', 0) or 0)
    p_reason = det.get('penalty_reason', '')

    cur.execute(\"\"\"
        INSERT INTO strategy_signal
            (ts_code, trade_date, track, composite_score, calibrated_score,
             scoring_strategy, season, gate_triggered,
             is_filtered, filter_reason, penalty_score, penalty_reason,
             short_term_score, stf_capital, stf_momentum, stf_overbought, stf_volume)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            track=VALUES(track), composite_score=VALUES(composite_score),
            calibrated_score=VALUES(calibrated_score),
            scoring_strategy=VALUES(scoring_strategy), season=VALUES(season),
            gate_triggered=VALUES(gate_triggered),
            is_filtered=VALUES(is_filtered), filter_reason=VALUES(filter_reason),
            penalty_score=VALUES(penalty_score), penalty_reason=VALUES(penalty_reason),
            short_term_score=VALUES(short_term_score),
            stf_capital=VALUES(stf_capital), stf_momentum=VALUES(stf_momentum),
            stf_overbought=VALUES(stf_overbought), stf_volume=VALUES(stf_volume)
    \"\"\", (
        code, '$TD', track,
        round(comp_score, 2), round(calib_score, 2),
        'dual_track_v1', judge['market_season'], 0,
        0, '',
        round(p_score, 2), p_reason,
        float(stf.get('short_term_score', 50) or 50),
        float(stf.get('capital_inertia', 50) or 50),
        float(stf.get('short_momentum', 50) or 50),
        float(stf.get('overbought_safety', 50) or 50),
        float(stf.get('volume_health', 50) or 50),
    ))
    saved += 1
    if saved % 200 == 0:
        conn.commit()

conn.commit()
cur.close()
conn.close()

# 验证
conn2 = get_connection()
cur2 = conn2.cursor()
cur2.execute(\"SELECT COUNT(*) as cnt FROM strategy_signal WHERE trade_date='$TD' AND scoring_strategy='dual_track_v1'\")
cnt = cur2.fetchone()['cnt']
cur2.close(); conn2.close()

print(f'✅ {saved}只写入 | 验证{cnt}条 | {elapsed:.0f}s | penalty有惩罚{p_score>0}')
" 2>&1 | tee -a "$LOG"

    echo "--- $TD 完成 ---" | tee -a "$LOG"
    sleep 1
done

echo ""
echo "===== 全部完成 ====="
echo "验证2026年完整度:"
mysql -u debian-sys-maint -p$(grep password /etc/mysql/debian.cnf | head -1 | awk '{print $3}') stock_db_v2 -e "
SELECT COUNT(DISTINCT trade_date) as 已写天数,
       SUM(CASE WHEN penalty_score>0 THEN 1 ELSE 0 END) as 有惩罚
FROM strategy_signal WHERE scoring_strategy='dual_track_v1' AND trade_date>='2026-01-01';
SELECT s.trade_date
FROM season_state s
LEFT JOIN strategy_signal sig ON s.trade_date = sig.trade_date AND sig.scoring_strategy='dual_track_v1'
WHERE s.index_code='MARKET' AND s.trade_date>='2026-01-01' AND s.trade_date<='2026-07-17'
  AND sig.trade_date IS NULL
ORDER BY s.trade_date;
" 2>/dev/null
