-- ============================================================
-- stock_db_v2 DDL — 股票智能分析管理系统 v2
-- 与 v1 完全隔离。核心8张 + 辅助12张 = 20张
-- ============================================================

-- 1. 股票基本信息
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code        VARCHAR(12)   PRIMARY KEY COMMENT '股票代码',
    name           VARCHAR(32)   NOT NULL     COMMENT '股票名称',
    industry       VARCHAR(64)   DEFAULT ''   COMMENT '所属行业',
    area           VARCHAR(32)   DEFAULT ''   COMMENT '地区',
    market         VARCHAR(16)   DEFAULT ''   COMMENT '市场类别',
    list_date      DATE          DEFAULT NULL COMMENT '上市日期',
    is_active      TINYINT(1)    DEFAULT 1    COMMENT '是否活跃',
    updated_at     DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_industry (industry),
    INDEX idx_market (market)
) ENGINE=InnoDB COMMENT='股票基本信息';

-- 2. 日K线
CREATE TABLE IF NOT EXISTS daily_kline (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code        VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    trade_date     DATE          NOT NULL     COMMENT '交易日',
    open           DECIMAL(12,2) DEFAULT 0    COMMENT '开盘价',
    high           DECIMAL(12,2) DEFAULT 0    COMMENT '最高价',
    low            DECIMAL(12,2) DEFAULT 0    COMMENT '最低价',
    close          DECIMAL(12,2) DEFAULT 0    COMMENT '收盘价',
    pre_close      DECIMAL(12,2) DEFAULT 0    COMMENT '昨收',
    change_pct     DECIMAL(6,2)  DEFAULT 0    COMMENT '涨跌幅 %',
    vol            DECIMAL(20,2) DEFAULT 0    COMMENT '成交量(手)',
    amount         DECIMAL(20,2) DEFAULT 0    COMMENT '成交额(万元)',
    UNIQUE KEY uk_stock_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_ts_code (ts_code)
) ENGINE=InnoDB COMMENT='日K线数据';

-- 3. 评分信号（★ 唯一评分源）
CREATE TABLE IF NOT EXISTS strategy_signal (
    id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code              VARCHAR(12)   NOT NULL    COMMENT '股票代码',
    trade_date           DATE          NOT NULL    COMMENT '交易日',
    -- 评分维度
    composite_score      DECIMAL(6,2)  DEFAULT 0   COMMENT '综合评分',
    raw_score            DECIMAL(6,2)  DEFAULT 0   COMMENT '场景裸分',
    trend_score          DECIMAL(6,2)  DEFAULT 0   COMMENT '趋势分',
    momentum_score       DECIMAL(6,2)  DEFAULT 0   COMMENT '动量分',
    structure_score      DECIMAL(6,2)  DEFAULT 0   COMMENT '结构分',
    emotion_score        DECIMAL(6,2)  DEFAULT 0   COMMENT '情绪分',
    -- 信号
    signal_type          VARCHAR(20)   DEFAULT 'HOLD' COMMENT '信号类型: STRONG_BUY/BUY/CAUTIOUS_BUY/HOLD/SELL',
    signal_label         VARCHAR(20)   DEFAULT ''     COMMENT '信号标签',
    direction            VARCHAR(10)   DEFAULT 'LONG' COMMENT '方向 LONG/SHORT',
    season               VARCHAR(20)   DEFAULT ''     COMMENT '当前季节',
    regime               VARCHAR(10)   DEFAULT ''     COMMENT '市场体制',
    -- 安全闸门
    gate_triggered       TINYINT(1)    DEFAULT 0      COMMENT '安全闸门触发',
    autumn_tiger         TINYINT(1)    DEFAULT 0      COMMENT '秋老虎标记',
    tiger_confidence     DECIMAL(6,2)  DEFAULT 0      COMMENT '秋老虎置信度',
    -- 仓位建议
    position_pct         DECIMAL(6,2)  DEFAULT 0      COMMENT '建议仓位%',
    -- 审核
    review_status        VARCHAR(20)   DEFAULT 'pending' COMMENT '审核状态',
    review_note          TEXT,
    is_calculable        TINYINT(1)    DEFAULT 1,
    created_at           DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_signal (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_signal_type (signal_type),
    INDEX idx_composite_score (composite_score),
    INDEX idx_direction (direction)
) ENGINE=InnoDB COMMENT='策略评分信号（唯一评分源）';

-- 4. 持仓
CREATE TABLE IF NOT EXISTS portfolio_holdings (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    name             VARCHAR(32)   NOT NULL     COMMENT '股票名称',
    shares           INT           DEFAULT 0    COMMENT '持股数量',
    cost_price       DECIMAL(12,4) DEFAULT 0    COMMENT '成本价(元)',
    current_price    DECIMAL(12,4) DEFAULT 0    COMMENT '现价(元)',
    buy_date         DATE          DEFAULT NULL COMMENT '建仓日期',
    position_ratio   DECIMAL(6,4)  DEFAULT 0    COMMENT '仓位占比',
    profit_pct       DECIMAL(8,2)  DEFAULT 0    COMMENT '盈亏百分比',
    profit_amount    DECIMAL(14,2) DEFAULT 0    COMMENT '盈亏金额',
    market_value     DECIMAL(14,2) DEFAULT 0    COMMENT '市值',
    status           VARCHAR(10)   DEFAULT 'hold' COMMENT 'hold/locked/closed',
    lock_reason      VARCHAR(255)  DEFAULT ''    COMMENT '锁定原因',
    notes            TEXT,
    updated_at       DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at       DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_ts_code (ts_code),
    INDEX idx_status (status)
) ENGINE=InnoDB COMMENT='持仓管理';

-- 5. 监控池
CREATE TABLE IF NOT EXISTS watch_pool (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    name             VARCHAR(32)   NOT NULL     COMMENT '股票名称',
    industry         VARCHAR(64)   DEFAULT ''   COMMENT '行业',
    reason           TEXT                        COMMENT '加入原因',
    is_active        TINYINT(1)    DEFAULT 1    COMMENT '是否活跃',
    added_at         DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_ts_code (ts_code),
    INDEX idx_is_active (is_active)
) ENGINE=InnoDB COMMENT='监控池';

-- 6. 市场季节状态
CREATE TABLE IF NOT EXISTS season_state (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    index_code       VARCHAR(12)   NOT NULL     COMMENT '指数代码',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    season           VARCHAR(20)   DEFAULT 'chaos' COMMENT '季节',
    raw_score        DECIMAL(6,2)  DEFAULT 0    COMMENT '场景裸分',
    confidence       DECIMAL(6,2)  DEFAULT 0    COMMENT '置信度',
    position_advice  VARCHAR(20)   DEFAULT ''    COMMENT '仓位建议',
    hengjiyuan_level VARCHAR(20)   DEFAULT ''    COMMENT '恒纪元级别',
    hengjiyuan_score DECIMAL(6,2)  DEFAULT 0    COMMENT '恒纪元分数',
    confidence_mult  DECIMAL(6,2)  DEFAULT 1.0  COMMENT '置信度乘数',
    updated_at       DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_index_date (index_code, trade_date),
    INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB COMMENT='市场季节状态';

-- 7. 缠论结构
CREATE TABLE IF NOT EXISTS chanlun_structure (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    trend_type       VARCHAR(20)   DEFAULT ''   COMMENT '趋势类型: up/down/sideways',
    structure_score  DECIMAL(6,2)  DEFAULT 0    COMMENT '结构评分',
    phase            VARCHAR(20)   DEFAULT ''   COMMENT '缠论阶段',
    confidence       DECIMAL(6,2)  DEFAULT 0    COMMENT '置信度',
    details          JSON                       COMMENT '详细结构数据',
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_trend_type (trend_type)
) ENGINE=InnoDB COMMENT='缠论结构分析';

-- 8. 技术指标
CREATE TABLE IF NOT EXISTS technical_indicator (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    ma5              DECIMAL(12,2) DEFAULT 0    COMMENT '5日均线',
    ma10             DECIMAL(12,2) DEFAULT 0    COMMENT '10日均线',
    ma20             DECIMAL(12,2) DEFAULT 0    COMMENT '20日均线',
    ma60             DECIMAL(12,2) DEFAULT 0    COMMENT '60日均线',
    vol_ma5          DECIMAL(20,2) DEFAULT 0    COMMENT '5日均量',
    vol_ma10         DECIMAL(20,2) DEFAULT 0    COMMENT '10日均量',
    rsi              DECIMAL(6,2)  DEFAULT 0    COMMENT 'RSI',
    macd_dif         DECIMAL(12,2) DEFAULT 0    COMMENT 'MACD DIF',
    macd_dea         DECIMAL(12,2) DEFAULT 0    COMMENT 'MACD DEA',
    macd_hist        DECIMAL(12,2) DEFAULT 0    COMMENT 'MACD 柱',
    kdj_k            DECIMAL(6,2)  DEFAULT 0    COMMENT 'KDJ K值',
    kdj_d            DECIMAL(6,2)  DEFAULT 0    COMMENT 'KDJ D值',
    kdj_j            DECIMAL(6,2)  DEFAULT 0    COMMENT 'KDJ J值',
    bb_upper         DECIMAL(12,2) DEFAULT 0    COMMENT '布林上轨',
    bb_middle        DECIMAL(12,2) DEFAULT 0    COMMENT '布林中轨',
    bb_lower         DECIMAL(12,2) DEFAULT 0    COMMENT '布林下轨',
    atr              DECIMAL(12,2) DEFAULT 0    COMMENT 'ATR',
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB COMMENT='技术指标';

-- 9. 行业板块映射
CREATE TABLE IF NOT EXISTS sector_mapping (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    sector_name      VARCHAR(64)   NOT NULL     COMMENT '板块名称',
    sector_code      VARCHAR(32)   NOT NULL     COMMENT '板块代码',
    industry_level   VARCHAR(10)   DEFAULT 'L2' COMMENT '行业级别',
    UNIQUE KEY uk_stock_sector (ts_code, sector_code),
    INDEX idx_sector_name (sector_name)
) ENGINE=InnoDB COMMENT='行业板块映射';

-- 10. 板块缠论缓存
CREATE TABLE IF NOT EXISTS sector_chanlun_cache (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    sector_name      VARCHAR(64)   NOT NULL     COMMENT '板块名称',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    trend_type       VARCHAR(20)   DEFAULT ''   COMMENT '趋势类型',
    structure_score  DECIMAL(6,2)  DEFAULT 0    COMMENT '结构评分',
    top_stocks       JSON                       COMMENT '头部股票TOP3',
    avg_change_pct   DECIMAL(6,2)  DEFAULT 0    COMMENT '平均涨幅',
    stock_count      INT           DEFAULT 0    COMMENT '板块内股票数量',
    UNIQUE KEY uk_sector_date (sector_name, trade_date),
    INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB COMMENT='板块缠论缓存';

-- 11. 资金流向
CREATE TABLE IF NOT EXISTS money_flow (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    main_net         DECIMAL(14,2) DEFAULT 0    COMMENT '主力净流入',
    retail_net       DECIMAL(14,2) DEFAULT 0    COMMENT '散户净流入',
    buy_value        DECIMAL(14,2) DEFAULT 0    COMMENT '买入额',
    sell_value       DECIMAL(14,2) DEFAULT 0    COMMENT '卖出额',
    net_value        DECIMAL(14,2) DEFAULT 0    COMMENT '净额',
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB COMMENT='资金流向';

-- 12. 股票备注
CREATE TABLE IF NOT EXISTS stock_notes (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    note_type        VARCHAR(20)   DEFAULT 'ai' COMMENT '类型: ai/manual',
    content          TEXT          NOT NULL     COMMENT '内容',
    created_at       DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_ts_code (ts_code),
    INDEX idx_note_type (note_type)
) ENGINE=InnoDB COMMENT='股票备注/AI分析';

-- 13. 策略配置
CREATE TABLE IF NOT EXISTS strategy_config (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    config_key       VARCHAR(64)   NOT NULL     COMMENT '配置键',
    config_value     TEXT          NOT NULL     COMMENT '配置值',
    description      VARCHAR(255)  DEFAULT ''   COMMENT '说明',
    updated_at       DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_config_key (config_key)
) ENGINE=InnoDB COMMENT='策略配置（含阈值）';

-- 14. 系统配置
CREATE TABLE IF NOT EXISTS system_config (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    config_key       VARCHAR(64)   NOT NULL     COMMENT '配置键',
    config_value     TEXT          NOT NULL     COMMENT '配置值',
    description      VARCHAR(255)  DEFAULT ''   COMMENT '说明',
    updated_at       DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_config_key (config_key)
) ENGINE=InnoDB COMMENT='系统配置';

-- 15. 管道执行日志
CREATE TABLE IF NOT EXISTS pipeline_exec_log (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    pipeline_name    VARCHAR(64)   NOT NULL     COMMENT '管道名称',
    step_name        VARCHAR(64)   DEFAULT ''   COMMENT '步骤名称',
    status           VARCHAR(16)   DEFAULT 'running' COMMENT 'running/success/failed',
    started_at       DATETIME      DEFAULT CURRENT_TIMESTAMP,
    finished_at      DATETIME      DEFAULT NULL,
    duration_sec     INT           DEFAULT 0    COMMENT '耗时秒',
    error_msg        TEXT          DEFAULT NULL COMMENT '错误信息',
    INDEX idx_pipeline (pipeline_name),
    INDEX idx_status (status),
    INDEX idx_started_at (started_at)
) ENGINE=InnoDB COMMENT='管道执行日志';

-- 16. 板块日线
CREATE TABLE IF NOT EXISTS sector_index_daily (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    sector_code      VARCHAR(32)   NOT NULL     COMMENT '板块代码',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    open             DECIMAL(12,2) DEFAULT 0,
    close            DECIMAL(12,2) DEFAULT 0,
    high             DECIMAL(12,2) DEFAULT 0,
    low              DECIMAL(12,2) DEFAULT 0,
    change_pct       DECIMAL(6,2)  DEFAULT 0    COMMENT '涨跌幅',
    vol              DECIMAL(20,2) DEFAULT 0,
    amount           DECIMAL(20,2) DEFAULT 0,
    UNIQUE KEY uk_sector_date (sector_code, trade_date),
    INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB COMMENT='板块日线';

-- 17. 持仓账户
CREATE TABLE IF NOT EXISTS portfolio_account (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    account_date     DATE          NOT NULL     COMMENT '日期',
    total_asset      DECIMAL(16,2) DEFAULT 0    COMMENT '总资产',
    market_value     DECIMAL(16,2) DEFAULT 0    COMMENT '市值',
    available_cash   DECIMAL(16,2) DEFAULT 0    COMMENT '可用资金',
    daily_profit     DECIMAL(14,2) DEFAULT 0    COMMENT '日收益',
    total_profit     DECIMAL(14,2) DEFAULT 0    COMMENT '累计收益',
    UNIQUE KEY uk_date (account_date),
    INDEX idx_account_date (account_date)
) ENGINE=InnoDB COMMENT='持仓账户汇总';

-- 18. 涨停池
CREATE TABLE IF NOT EXISTS limit_up_daily (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    name             VARCHAR(32)   NOT NULL     COMMENT '股票名称',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    limit_up_time    TIME          DEFAULT NULL COMMENT '封板时间',
    open_times       INT           DEFAULT 0    COMMENT '开板次数',
    sealed           TINYINT(1)    DEFAULT 1    COMMENT '是否封住',
    change_pct       DECIMAL(6,2)  DEFAULT 0    COMMENT '涨幅',
    turnover_rate    DECIMAL(6,2)  DEFAULT 0    COMMENT '换手率',
    reason           VARCHAR(255)  DEFAULT ''   COMMENT '涨停原因',
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB COMMENT='涨停数据';

-- 19. 快照缓存（从 strategy_signal 生成）
CREATE TABLE IF NOT EXISTS watch_pool_snapshot (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    v_score          DECIMAL(6,2)  DEFAULT 0    COMMENT '评分',
    raw_score        DECIMAL(6,2)  DEFAULT 0    COMMENT '裸分',
    trend_score      DECIMAL(6,2)  DEFAULT 0    COMMENT '趋势分',
    momentum_score   DECIMAL(6,2)  DEFAULT 0    COMMENT '动量分',
    signal_type      VARCHAR(20)   DEFAULT 'HOLD',
    signal_label     VARCHAR(20)   DEFAULT '',
    season           VARCHAR(20)   DEFAULT '',
    regime           VARCHAR(10)   DEFAULT '',
    name             VARCHAR(32)   DEFAULT ''   COMMENT '股票名称',
    industry         VARCHAR(64)   DEFAULT ''   COMMENT '行业',
    close_price      DECIMAL(12,2) DEFAULT 0    COMMENT '收盘价',
    change_pct       DECIMAL(6,2)  DEFAULT 0    COMMENT '涨跌幅',
    position_pct     DECIMAL(6,2)  DEFAULT 0    COMMENT '建议仓位',
    ret_5d           DECIMAL(6,2)  DEFAULT 0    COMMENT '5日收益',
    ret_10d          DECIMAL(6,2)  DEFAULT 0    COMMENT '10日收益',
    ret_20d          DECIMAL(6,2)  DEFAULT 0    COMMENT '20日收益',
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_v_score (v_score DESC)
) ENGINE=InnoDB COMMENT='监控池快照（从strategy_signal同步）';

-- 20. 前复权K线
CREATE TABLE IF NOT EXISTS daily_kline_qfq (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code          VARCHAR(12)   NOT NULL     COMMENT '股票代码',
    trade_date       DATE          NOT NULL     COMMENT '交易日',
    open             DECIMAL(12,2) DEFAULT 0,
    high             DECIMAL(12,2) DEFAULT 0,
    low              DECIMAL(12,2) DEFAULT 0,
    close            DECIMAL(12,2) DEFAULT 0,
    pre_close        DECIMAL(12,2) DEFAULT 0,
    change_pct       DECIMAL(6,2)  DEFAULT 0,
    vol              DECIMAL(20,2) DEFAULT 0,
    amount           DECIMAL(20,2) DEFAULT 0,
    qfq_factor       DECIMAL(12,6) DEFAULT 1    COMMENT '复权因子',
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB COMMENT='前复权K线';

-- ============================================================
-- 初始数据：策略配置
-- ============================================================
INSERT INTO strategy_config (config_key, config_value, description) VALUES
('buy_min_score', '75', '买入最低评分'),
('strong_buy_threshold', '80', 'STRONG_BUY阈值'),
('buy_threshold', '75', 'BUY阈值'),
('cautious_buy_threshold', '40', 'CAUTIOUS_BUY阈值'),
('hold_threshold', '20', 'HOLD阈值'),
('sell_threshold', '0', 'SELL阈值'),
('trailing_stop_pct', '15.0', '移动止盈回撤%'),
('max_hold_days', '30', '最长持有天数'),
('cool_days', '20', '冷却天数'),
('p1_check_day', '5', 'P1检查点(交易日)'),
('p2_check_day', '15', 'P2检查点(交易日)'),
('p3_check_day', '25', 'P3检查点(交易日)'),
('p4_close_day', '30', 'P4强制平仓(交易日)'),
('stop_loss_pct', '-10.0', '止损%'),
('position_max_pct', '50.0', '单票最大仓位%'),
('gate_enabled', '1', '安全闸门启用'),
('summer_buy_threshold', '45', '夏季买入阈值'),
('autumn_buy_threshold', '38', '秋季买入阈值'),
('winter_buy_threshold', '40', '冬季买入阈值'),
('spring_buy_threshold', '45', '春季买入阈值'),
('chaos_buy_threshold', '48', '混沌期买入阈值');

INSERT INTO system_config (config_key, config_value, description) VALUES
('api_key', 'a1b2c3d4e5f6', 'API认证密钥'),
('pipeline_time', '17:00', '管道每日执行时间'),
('tushare_token', '', 'Tushare API Token（通过environ覆盖）'),
('data_error_marker', '-1', '数据异常标记'),
('pipeline_lock_file', '/tmp/stock_pipeline_v2.lock', '管道锁文件路径');

-- ============================================================
-- 结束
-- ============================================================
