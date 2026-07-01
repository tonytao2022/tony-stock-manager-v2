"""
routes/ai_analysis.py - AI个股分析 & 分析备注查询
为V2前端提供:
  POST /api/v2/ai/analyze  - 执行AI分析(写入报告并返回)
  GET  /api/v2/stock-notes  - 查询历史分析记录(支持多种过滤)

兼容V1前端AI分析页面的接口格式。
"""
import json, logging, subprocess, os
from datetime import datetime, timedelta
from flask import Blueprint, request
from db_config import db_cursor, api_success, api_error, serialize_rows, get_connection
from auth import require_auth

logger = logging.getLogger('ai_analysis')
ai_bp = Blueprint('ai_analysis', __name__)


# ─── 工具函数 ────────────────────────────────────────────────

def _get_deepseek_api_key():
    """从MySQL读取DeepSeek API Key"""
    try:
        import pymysql
        cfg = {
            'host': '127.0.0.1', 'port': 3306, 'user': 'debian-sys-maint',
            'password': __import__('re').search(r'password\s*=\s*(\S+)', open('/etc/mysql/debian.cnf').read()).group(1),
            'database': 'openclaw_config', 'charset': 'utf8mb4',
            'cursorclass': pymysql.cursors.DictCursor
        }
        conn = pymysql.connect(**cfg)
        cur = conn.cursor()
        cur.execute("SELECT api_key FROM api_credentials WHERE id=10 AND is_active=1 LIMIT 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row.get('api_key'):
            return row['api_key']
    except Exception as e:
        print(f'读取DeepSeek Key失败: {e}')
    return os.environ.get('DEEPSEEK_API_KEY', '')


def _fetch_stock_data(ts_code):
    """采集个股的多维度数据用于构造提示词 - 通过Tushare API获取"""
    result = {}

    try:
        # 从MySQL读取Tushare Token
        import re as _re
        import pymysql
        pwd = _re.search(r'password\s*=\s*(\S+)', open('/etc/mysql/debian.cnf').read()).group(1)
        _conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
            password=pwd, database='openclaw_config', charset='utf8mb4')
        _cur = _conn.cursor()
        _cur.execute("SELECT api_key FROM api_credentials WHERE id=1 AND is_active=1 LIMIT 1")
        _row = _cur.fetchone()
        tushare_token = _row[0] if _row else ''
        _cur.close()
        _conn.close()
    except:
        tushare_token = os.environ.get('TUSHARE_TOKEN', '')

    import tushare as ts
    pro = ts.pro_api(tushare_token)

    # 1. 股票基础信息
    try:
        df = pro.stock_basic(ts_code=ts_code)
        if df is not None and not df.empty:
            r = df.iloc[0]
            result['name'] = r.get('name', ts_code)
            result['industry'] = r.get('industry', '未知')
            result['area'] = r.get('area', '未知')
            result['market'] = r.get('market', '未知')
            result['list_date'] = r.get('list_date', '未知')
        else:
            result['name'] = ts_code
            result['industry'] = '未知'
            result['area'] = '未知'
            result['market'] = '未知'
            result['list_date'] = '未知'
    except:
        # 降级到本地数据库
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT name, industry, area, market FROM stock_basic WHERE ts_code=%s LIMIT 1", (ts_code,))
            row = cur.fetchone()
            if row:
                result['name'] = row.get('name', ts_code)
                result['industry'] = row.get('industry', '未知')
                result['area'] = row.get('area', '未知')
                result['market'] = row.get('market', '未知')
            else:
                result['name'] = ts_code
                result['industry'] = '未知'
                result['area'] = '未知'
                result['market'] = '未知'
            result['list_date'] = '未知'

    # 2. 最近30日交易数据
    try:
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        df_daily = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df_daily is not None and not df_daily.empty:
            result['daily_data'] = df_daily.to_string(index=False)
            # 最新价
            latest = df_daily.iloc[0]
            result['price'] = latest.get('close', 0)
            result['change'] = latest.get('pct_chg', 0)
            result['volume'] = latest.get('vol', 0)
            result['amount'] = latest.get('amount', 0)
            result['high'] = latest.get('high', 0)
            result['low'] = latest.get('low', 0)
            result['open'] = latest.get('open', 0)
            result['pre_close'] = latest.get('pre_close', 0)
        else:
            result['daily_data'] = '无数据'
            result['price'] = '未知'
            result['change'] = '未知'
            result['volume'] = '未知'
            result['amount'] = '未知'
    except:
        result['daily_data'] = '无数据'
        result['price'] = '未知'
        result['change'] = '未知'
        result['volume'] = '未知'
        result['amount'] = '未知'

    # 3. 主要指标(市盈率/市净率/总市值) — 优先从数据库取最新交易日，没有再调Tushare
    try:
        from db_config import db_cursor
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT pe, pb, total_mv, circ_mv FROM daily_basic WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1", (ts_code,))
            r = cur.fetchone()
        if r and r['pe']:
            result['pe'] = float(r['pe'])
            result['pb'] = float(r['pb']) if r['pb'] else '未知'
            result['total_mv'] = float(r['total_mv']) if r['total_mv'] else '未知'
        else:
            # fallback: Tushare接口
            df_dailybasic = pro.daily_basic(ts_code=ts_code, trade_date=datetime.now().strftime('%Y%m%d'))
            if df_dailybasic is not None and not df_dailybasic.empty:
                r = df_dailybasic.iloc[0]
                result['pe'] = r.get('pe', '未知')
                result['pb'] = r.get('pb', '未知')
                result['total_mv'] = r.get('total_mv', '未知')
            else:
                result['pe'] = '未知'
                result['pb'] = '未知'
                result['total_mv'] = '未知'
    except:
        result['pe'] = '未知'
        result['pb'] = '未知'
        result['total_mv'] = '未知'

    return result


def _build_prompt(ts_code, data):
    """根据个股数据构造DeepSeek提示词 - 14维度分析模板"""
    name = data.get('name', ts_code)
    industry = data.get('industry', '未知')
    area = data.get('area', '未知')
    market = data.get('market', '未知')
    list_date = data.get('list_date', '未知')
    price = data.get('price', '未知')
    change = data.get('change', '未知')
    volume = data.get('volume', '未知')
    amount = data.get('amount', '未知')
    pe = data.get('pe', '未知')
    pb = data.get('pb', '未知')
    total_mv = data.get('total_mv', '未知')
    daily_data = data.get('daily_data', '无数据')

    prompt = f"""如何分析一只股票,以{ts_code}为例,我对这只股票一无所知,如何尽可能全面的分析它,并给出初步的趋势预判。

基本面信息:
- 公司名称:{name}
- 所属行业:{industry}
- 上市日期:{list_date}
- 所在地区:{area}
- 市场类型:{market}

最新交易数据:
- 最新价:{price}元
- 涨跌幅:{change}%
- 成交量:{volume}手
- 成交额:{amount}千元

主要指标:
- 市盈率:{pe}
- 市净率:{pb}
- 总市值:{total_mv}元

最近交易数据:
{daily_data}

请基于以上真实数据,给出专业、客观、全面的分析报告。请严格按照以下格式进行分析,每个部分必须以对应的标题开头,要求数据准确、真实,总结精炼到位:

1. 基本面分析:
详细分析公司基础信息与行业定位、业务与成长性分析、财务状况等

2. 整体基本面分析:
详细分析公司公司的主要业模式、市场份额和盈利来源,并说明其核心竞争力是什么

3.评估财务健康状况:
详细审视公司最近几个季的财务指标表现,包括收入、净利润、毛利率和负债情况,并简要分析这些数据的趋势

4. 技术面与资金动向分析:
详细分析价格趋势、成交量、主要技术指标等

5. 分析历史股价走势与波动:
对公司的历史股价走势进行区熊市期回顾,关注其在过去牛市的表表现,并说明波动率和主要驱动因素

6. 宏观经济及行业环境影响：
详细分析当前宏观环境中哪些因最可能影响公司的业绩，包括利率政策、行业监管、国际形势等并解释可能的正面或负面影响

7. 风险评估：
详细分析市场风险、行业风险、公司特定风险等

8. 市场情绪及媒体舆论：
查看近期对公司的新闻、分师报告和社交媒体言论，归纳当前市场情绪属于看涨还是看跌，并说明背后的主要原因

9. 公司最新财报或重大公告：
针对公司最新发布的季度/年度财报进行详细解读，包括收入、净利润、现金流和各项业务部门表现，并说明与市场预期的差异

10. 股票关键词：
给出该股在爬雪球类似平台讨论量激增的股票关键词

11. 研判未来成长潜力与风险：
列出公司未来 1-3 年内可能的主要增长驱动因素，以及该公司面临的潜在风险或不确定性，并估其长期发展前景

12. 投资建议：
给出明确的趋势预判与策略建议

13. 综合观点与投资建议：
基于对公司的业务模式财务数据、行业比较和风险分析为价值投资者和短期投机者分别绍出决策意见，并阐述理由

请用中文回答,每个部分至少200字。请确保严格按照上述格式回复,包含所有标题,移除所有无关内容如参考文献。
"""
    return prompt


def _call_deepseek(prompt):
    """调用DeepSeek API生成分析报告"""
    api_key = _get_deepseek_api_key()
    if not api_key:
        return None

    import requests
    try:
        resp = requests.post(
            'https://api.deepseek.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': 'deepseek-chat',
                'messages': [
                    {'role': 'system', 'content': '你是一位专业的A股股票分析师。请基于提供的真实数据,给出专业、客观、全面的分析报告。分析要贴合中国A股市场实际,不提供虚假信息。'},
                    {'role': 'user', 'content': prompt},
                ],
                'temperature': 0.3,
                'max_tokens': 8192,
            },
            timeout=60
        )
        if resp.status_code == 200:
            result = resp.json()
            return result['choices'][0]['message']['content']
        else:
            logger.error(f'DeepSeek API error: {resp.status_code} {resp.text[:300]}')
            return None
    except Exception as e:
        logger.error(f'DeepSeek call failed: {e}')
        return None


def _data_sources(data):
    """标记数据是否已采集(Tushare数据源)"""
    return {
        '基本面信息': '✅ 已采集',
        '最近30日交易数据': '✅' if data.get('daily_data') and data.get('daily_data') != '无数据' else '❌',
        '市盈率/市净率': '✅' if data.get('pe') != '未知' else '❌',
        '最新行情': '✅' if data.get('price') != '未知' else '❌',
    }


# ─── 路由:POST /ai/analyze ─────────────────────────────────

@ai_bp.route('/ai/analyze', methods=['POST'])
@require_auth
def ai_analyze():
    """
    执行AI个股分析
    请求: { "ts_code": "300476.SZ" }
    响应: { code:0, data:{ ts_code, name, report, note_date, score:{composite_score,...}, data_sources:{...}, latest_close } }
    """
    try:
        data = request.get_json(silent=True) or {}
        ts_code = data.get('ts_code', '').strip().upper()
        if not ts_code:
            return api_error('缺少参数 ts_code', http_status=400)

        # 查询股票信息
        name = ts_code
        latest_close = None
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT name, close FROM stock_basic sb LEFT JOIN daily_kline dk ON sb.ts_code=dk.ts_code AND dk.trade_date=(SELECT MAX(trade_date) FROM daily_kline) WHERE sb.ts_code=%s LIMIT 1", (ts_code,))
            row = cur.fetchone()
            if row:
                name = row.get('name', ts_code)
                latest_close = float(row.get('close', 0)) if row.get('close') else None

        # 采集多维度数据
        stock_data = _fetch_stock_data(ts_code)

        # 构造提示词并调用DeepSeek
        prompt = _build_prompt(ts_code, stock_data)
        report_content = _call_deepseek(prompt)

        # 如果DeepSeek调用失败,降级为本地生成
        if not report_content:
            logger.warning(f'DeepSeek API调用失败,{ts_code}使用本地生成报告')
            report_content = _build_local_report(ts_code, stock_data)

        # 提取摘要(前120字符)
        summary = report_content.strip().replace('\n', ' ')[:120] if report_content else ''

        # 综合评分(从策略信号取)
        composite_score = None
        score_data = stock_data.get('score', {})
        if score_data and score_data.get('composite_score'):
            composite_score = score_data['composite_score']

        note_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data_sources = _data_sources(stock_data)

        # 写入数据库
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ai_analysis_reports
                    (ts_code, name, note_date, report_type, full_report, summary, latest_close, composite_score, data_sources)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                ts_code, name, note_date, 'AI_ANALYSIS',
                report_content or '', summary,
                float(latest_close) if latest_close else None,
                composite_score,
                json.dumps(data_sources, ensure_ascii=False)
            ))

        return api_success({
            'ts_code': ts_code,
            'name': name,
            'report': report_content,
            'note_date': note_date,
            'summary': summary,
            'latest_close': float(latest_close) if latest_close else None,
            'score': {
                'composite_score': int(composite_score) if composite_score else 0,
            },
            'data_sources': data_sources,
        })

    except Exception as e:
        logger.exception("AI analyze error")
        return api_error(str(e))


# ─── 路由:GET /stock-notes ──────────────────────────────────

@ai_bp.route('/stock-notes', methods=['GET'])
@require_auth
def query_stock_notes():
    """
    查询AI分析历史记录(兼容V1前端格式)
    参数:
      ts_code   - 股票代码(可选)
      name      - 股票名称(可选)
      limit     - 每页数量(默认15)
      page      - 页码(默认1)
      date_from - 开始日期
      date_to   - 结束日期
    响应: { code:0, data:{ notes:[...], total, page, limit } }
    """
    try:
        ts_code = request.args.get('ts_code', '').strip()
        name = request.args.get('name', '').strip()
        limit = min(int(request.args.get('limit', 15)), 100)
        page = max(int(request.args.get('page', 1)), 1)
        date_from = request.args.get('date_from', '')
        date_to = request.args.get('date_to', '')
        offset = (page - 1) * limit

        conditions = []
        params = []

        if ts_code:
            conditions.append("a.ts_code = %s")
            params.append(ts_code)
        if name:
            conditions.append("a.name LIKE %s")
            params.append(f'%{name}%')
        if date_from:
            conditions.append("a.note_date >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("a.note_date <= %s")
            params.append(date_to + ' 23:59:59')

        where = ' AND '.join(conditions) if conditions else '1=1'

        with db_cursor(commit=False) as cur:
            # 总数
            cur.execute(f"SELECT COUNT(*) as cnt FROM ai_analysis_reports a WHERE {where}", params)
            total = cur.fetchone()['cnt']

            # 分页查询
            cur.execute(f"""
                SELECT a.ts_code, a.name, a.note_date, a.report_type,
                       a.full_report, a.summary, a.latest_close, a.composite_score
                FROM ai_analysis_reports a
                WHERE {where}
                ORDER BY a.note_date DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            rows = cur.fetchall()

        notes = []
        for r in rows:
            note = {
                'ts_code': r['ts_code'],
                'name': r['name'],
                'note_date': r['note_date'].isoformat() if hasattr(r['note_date'], 'isoformat') else str(r['note_date']),
                'report_type': r.get('report_type', 'AI_ANALYSIS'),
                'full_report': r.get('full_report', ''),
                'summary': r.get('summary', ''),
                'latest_close': float(r['latest_close']) if r.get('latest_close') else None,
                'composite_score': int(r['composite_score']) if r.get('composite_score') else None,
            }
            notes.append(note)

        return api_success({
            'notes': notes,
            'total': total,
            'page': page,
            'limit': limit,
        })

    except Exception as e:
        logger.exception("stock-notes query error")
        return api_error(str(e))


# ─── 路由:POST /stock-notes (新建备注) ──────────────────────

@ai_bp.route('/stock-notes', methods=['POST'])
@require_auth
def create_stock_note():
    """
    新建分析备注/AI分析记录
    请求: { ts_code, name, report_type, full_report, summary, latest_close, composite_score }
    """
    try:
        data = request.get_json(silent=True) or {}
        ts_code = data.get('ts_code', '').strip().upper()
        if not ts_code:
            return api_error('缺少 ts_code', http_status=400)

        name = data.get('name', ts_code)
        report_type = data.get('report_type', 'AI_ANALYSIS')
        full_report = data.get('full_report', '')
        summary = data.get('summary', (full_report or '')[:120])

        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ai_analysis_reports
                    (ts_code, name, report_type, full_report, summary)
                VALUES (%s, %s, %s, %s, %s)
            """, (ts_code, name, report_type, full_report, summary))

        return api_success({'ts_code': ts_code, 'name': name})

    except Exception as e:
        return api_error(str(e))

def _build_local_report(ts_code, data):
    """DeepSeek不可用时,用数据库数据本地生成报告"""
    name = data.get('name', ts_code)
    kline = data.get('kline', {})
    trend = data.get('trend_20d', {})
    score = data.get('score', {})
    season = data.get('market_season', '未知')
    mf = data.get('moneyflow', [])
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    report = f"📊 {name}({ts_code}) 本地分析报告\n"
    report += f"分析时间:{now}\n\n"
    report += f"【市场环境】当前市场季节:{season}\n\n"

    if kline:
        report += f"【最新行情】{kline.get('trade_date')} 收盘{kline.get('close')} 涨跌{kline.get('change_pct')}%\n"
    if trend:
        t = trend
        report += f"【近期趋势】20日涨幅{t.get('change_pct_20d')}% 最高{t.get('max_close')} 最低{t.get('min_close')}\n"
        report += "趋势:" + ("上涨📈" if t.get('change_pct_20d', 0) > 5 else "震荡➡️" if t.get('change_pct_20d', 0) > -5 else "下跌📉") + "\n"
    if score:
        s = score
        report += f"【策略评分】校准分{s.get('calibrated_score','N/A')} 趋势{s.get('trend_score','N/A')} 动量{s.get('momentum_score','N/A')}\n"
    if mf:
        net = mf[0].get('net', 0)
        report += f"【资金流向】最新净流入{net/10000:.0f}万 " + ("🟢" if net > 0 else "🔴") + "\n"
    report += "\n【综合评级】基于系统多维度数据自动生成,仅供参考。\n"
    return report
