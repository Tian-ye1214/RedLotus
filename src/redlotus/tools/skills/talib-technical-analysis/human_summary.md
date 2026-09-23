# Human Summary

> I help you build quant strategies on A-share with ZVT — from data fetch to backtest, one flow. Just tell me what you want; I'll write the code, you don't have to dig docs. (Heads up: ZVT natively supports A-share, HK, and crypto. US stocks — stockus_nasdaq_AAPL — are half-baked; don't bother for serious work.)

## What I Can Do

- **tagline**: I help you build quant strategies on A-share with ZVT — from data fetch to backtest, one flow. Just tell me what you want; I'll write the code, you don't have to dig docs. (Heads up: ZVT natively supports A-share, HK, and crypto. US stocks — stockus_nasdaq_AAPL — are half-baked; don't bother for serious work.)
- **use_cases**: ['TA-Lib Documentation HTML Generator', 'A-share MACD daily golden-cross backtest with hfq price adjustment from eastmoney', 'End-to-end ZVT pipeline: FinanceRecorder + GoodCompanyFactor + StockTrader', 'Multi-factor strategy with TargetSelector (AND mode) combining MACD + volume breakout', 'Index composition data collection (SZ1000, SZ2000) with EM recorder', 'Institutional fund holdings tracker via joinquant_fund_runner pattern', 'Custom Transformer + Accumulator factor with per-entity rolling state']

## What I Ask You

- Target market: A-share (default), HK, or crypto? (US stocks in ZVT are half-baked — stockus_nasdaq_AAPL exists but coverage is thin)
- Data source / provider: eastmoney (free, no account), joinquant (account+paid), baostock (free, good history), akshare, or qmt (broker)?
- Strategy type: MACD golden-cross, MA crossover, volume breakout, fundamental screen, or custom factor?
- Time range: start_timestamp and end_timestamp for backtest period
- Target entity IDs: specific stocks (stock_sh_600000) or index components (SZ1000)?
