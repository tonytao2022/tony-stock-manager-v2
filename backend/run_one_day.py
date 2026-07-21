"""跑一天评分并写入DB，调用完退出释放内存"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['SKIP_REALTIME'] = '1'

from p6_dual_track_engine import batch_score, MarketContext
from db_config import get_connection

def main():
    td = sys.argv[1]
    conn = get_connection()
    cur = conn.cursor()
    
    # 读取季节数据
    cur.execute("""
        SELECT season, regime, confidence, raw_score, hengjiyuan_level,
               hengjiyuan_score, chaos_subtype, regime_strength, scoring_strategy
        FROM season_state
        WHERE index_code='MARKET' AND trade_date=%s
    """, (td,))
    row = cur.fetchone()
    if not row:
        print(f"❌ {td} 无季节数据")
        sys.exit(1)
    
    judge = {
        'market_season': row['season'],
        'market_regime': row['regime'],
        'market_confidence': float(row['confidence'] or 0.5),
        'market_scoring_strategy': row.get('scoring_strategy') or 'momentum',
        'trade_date': td,
        'market_raw_score': float(row['raw_score'] or 50),
        'hengjiyuan_level': row['hengjiyuan_level'] or 'weak_heng',
        'hengjiyuan_score': float(row['hengjiyuan_score'] or 50),
        'chaos_subtype': row['chaos_subtype'],
        'regime_strength': float(row['regime_strength'] or 1.0),
    }
    ctx = MarketContext(judge)
    
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    
    print(f"📊 {td} [{judge['market_season']}] {len(codes)}只", flush=True)
    
    t0 = time.time()
    results = batch_score(codes, ctx)
    elapsed = time.time() - t0
    print(f"   评分完成: {len(results)}只 ({elapsed:.0f}s)", flush=True)
    
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
        
        cur.execute("""
            INSERT INTO strategy_signal
                (ts_code, trade_date, track, composite_score, calibrated_score,
                 scoring_strategy, season, gate_triggered,
                 penalty_score, penalty_reason,
                 short_term_score, stf_capital, stf_momentum, stf_overbought, stf_volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                track=VALUES(track), composite_score=VALUES(composite_score),
                calibrated_score=VALUES(calibrated_score),
                scoring_strategy=VALUES(scoring_strategy), season=VALUES(season),
                penalty_score=VALUES(penalty_score), penalty_reason=VALUES(penalty_reason),
                short_term_score=VALUES(short_term_score),
                stf_capital=VALUES(stf_capital), stf_momentum=VALUES(stf_momentum),
                stf_overbought=VALUES(stf_overbought), stf_volume=VALUES(stf_volume)
        """, (
            code, td, track, round(comp_score, 2), round(calib_score, 2),
            'dual_track_v1', judge['market_season'], 0,
            round(p_score, 2), p_reason,
            float(stf.get('short_term_score', 50)),
            float(stf.get('capital_inertia', 50)),
            float(stf.get('short_momentum', 50)),
            float(stf.get('overbought_safety', 50)),
            float(stf.get('volume_health', 50)),
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
    cur2.execute("SELECT COUNT(*) as cnt FROM strategy_signal WHERE trade_date=%s AND scoring_strategy='dual_track_v1'", (td,))
    verify_cnt = cur2.fetchone()['cnt']
    cur2.close(); conn2.close()
    
    print(f"✅ {saved}只写入, 验证{verify_cnt}条", flush=True)
    
    # 有惩罚的示例
    p_examples = [r for r in results if r.get('details',{}).get('penalty_score',0) > 0][:3]
    if p_examples:
        for r in p_examples:
            det = r.get('details',{}) or {}
            print(f"  惩罚示例: {r['ts_code']} score={r['score']:.1f} penalty={det.get('penalty_score',0)} -> {det.get('penalty_reason','')[:60]}", flush=True)

if __name__ == '__main__':
    main()
