#!/usr/bin/env python3
"""
Crypto Backtesting Engine
Supports: EMA crossover, RSI, MACD, Bollinger Bands
Data source: Any ccxt-supported exchange
"""
import argparse
import json
import ccxt


# ═══════════════════════════════════════════
# Indicators
# ═══════════════════════════════════════════

def calc_ema(closes: list, span: int) -> list:
    ema = [closes[0]]
    k = 2 / (span + 1)
    for c in closes[1:]:
        ema.append(c * k + ema[-1] * (1 - k))
    return ema


def calc_sma(closes: list, period: int) -> list:
    sma = [None] * (period - 1)
    for i in range(period - 1, len(closes)):
        sma.append(sum(closes[i - period + 1:i + 1]) / period)
    return sma


def calc_rsi(closes: list, period: int = 14) -> list:
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    rsi = [50.0] * period
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period, len(deltas)):
        d = deltas[i]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
        rs = avg_gain / avg_loss if avg_loss else float("inf")
        rsi.append(100 - (100 / (1 + rs)))
    return rsi


def calc_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = calc_ema(macd_line[slow:], signal)
    # Pad signal to align with macd_line
    signal_line = [0.0] * slow + signal_line
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def calc_bbands(closes: list, period: int = 20, std_mult: float = 2.0):
    sma = calc_sma(closes, period)
    upper, lower = [], []
    for i in range(len(closes)):
        if sma[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            window = closes[i - period + 1:i + 1]
            std = (sum((x - sma[i]) ** 2 for x in window) / period) ** 0.5
            upper.append(sma[i] + std_mult * std)
            lower.append(sma[i] - std_mult * std)
    return upper, sma, lower


# ═══════════════════════════════════════════
# Data Fetching
# ═══════════════════════════════════════════

def fetch_candles(symbol: str, timeframe: str = "1h", limit: int = 1000,
                  exchange_id: str = "bybit") -> list:
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    candles = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return [{"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]} for c in candles]


# ═══════════════════════════════════════════
# Backtesting Core
# ═══════════════════════════════════════════

class BacktestResult:
    def __init__(self, strategy: str, params: dict):
        self.strategy = strategy
        self.params = params
        self.trades = []
        self.initial_capital = 0
        self.final_balance = 0

    def add_trade(self, pnl: float, reason: str, entry_price: float = 0, exit_price: float = 0, side: str = ""):
        self.trades.append({"pnl": round(pnl, 4), "reason": reason,
                            "entry": entry_price, "exit": exit_price, "side": side})

    def summary(self) -> dict:
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in self.trades)
        
        # Max drawdown
        peak = self.initial_capital
        max_dd = 0
        balance = self.initial_capital
        for t in self.trades:
            balance += t["pnl"]
            peak = max(peak, balance)
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        return {
            "strategy": self.strategy,
            "params": self.params,
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self.trades) * 100, 1) if self.trades else 0,
            "total_pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / self.initial_capital * 100, 1) if self.initial_capital else 0,
            "max_drawdown_pct": round(max_dd, 1),
            "best_trade": round(max((t["pnl"] for t in self.trades), default=0), 2),
            "worst_trade": round(min((t["pnl"] for t in self.trades), default=0), 2),
            "profit_factor": round(sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 2) if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"),
            "initial_capital": self.initial_capital,
            "final_balance": round(self.final_balance, 2),
        }


def _run_backtest(candles, strategy_fn, capital, leverage, pos_pct, sl_pct, tp_pct, fee_rate, strategy_name, params):
    closes = [c["c"] for c in candles]
    result = BacktestResult(strategy_name, params)
    result.initial_capital = capital
    bal = capital
    position = None

    signals = strategy_fn(closes)  # returns list of ("long"|"short"|"close"|None, index)

    for i in range(len(closes)):
        price = closes[i]

        # Check SL/TP
        if position:
            hit = False
            if position["side"] == "long":
                if price <= position["sl"]:
                    pnl = (position["sl"] - position["entry"]) / position["entry"] * position["margin"] * leverage - position["fee"]
                    bal += pnl; result.add_trade(pnl, "SL", position["entry"], position["sl"], "long"); position = None; hit = True
                elif price >= position["tp"]:
                    pnl = (position["tp"] - position["entry"]) / position["entry"] * position["margin"] * leverage - position["fee"]
                    bal += pnl; result.add_trade(pnl, "TP", position["entry"], position["tp"], "long"); position = None; hit = True
            else:
                if price >= position["sl"]:
                    pnl = (position["entry"] - position["sl"]) / position["entry"] * position["margin"] * leverage - position["fee"]
                    bal += pnl; result.add_trade(pnl, "SL", position["entry"], position["sl"], "short"); position = None; hit = True
                elif price <= position["tp"]:
                    pnl = (position["entry"] - position["tp"]) / position["entry"] * position["margin"] * leverage - position["fee"]
                    bal += pnl; result.add_trade(pnl, "TP", position["entry"], position["tp"], "short"); position = None; hit = True
            if hit:
                continue

        sig = signals[i] if i < len(signals) else None

        if position and sig == "close":
            if position["side"] == "long":
                pnl = (price - position["entry"]) / position["entry"] * position["margin"] * leverage - position["fee"]
            else:
                pnl = (position["entry"] - price) / position["entry"] * position["margin"] * leverage - position["fee"]
            bal += pnl
            result.add_trade(pnl, "signal", position["entry"], price, position["side"])
            position = None

        elif not position and sig in ("long", "short"):
            margin = bal * pos_pct
            fee = margin * leverage * fee_rate
            if sig == "long":
                position = {"side": "long", "entry": price, "margin": margin, "fee": fee,
                            "sl": price * (1 - sl_pct), "tp": price * (1 + tp_pct)}
            else:
                position = {"side": "short", "entry": price, "margin": margin, "fee": fee,
                            "sl": price * (1 + sl_pct), "tp": price * (1 - tp_pct)}

    # Close any remaining position
    if position:
        price = closes[-1]
        if position["side"] == "long":
            pnl = (price - position["entry"]) / position["entry"] * position["margin"] * leverage - position["fee"]
        else:
            pnl = (position["entry"] - price) / position["entry"] * position["margin"] * leverage - position["fee"]
        bal += pnl
        result.add_trade(pnl, "end", position["entry"], price, position["side"])

    result.final_balance = bal
    return result


# ═══════════════════════════════════════════
# Strategy Signal Generators
# ═══════════════════════════════════════════

def make_ema_signals(fast=12, slow=26):
    def fn(closes):
        ema_f = calc_ema(closes, fast)
        ema_s = calc_ema(closes, slow)
        signals = [None] * len(closes)
        pos = None
        for i in range(slow + 1, len(closes)):
            golden = ema_f[i - 1] <= ema_s[i - 1] and ema_f[i] > ema_s[i]
            death = ema_f[i - 1] >= ema_s[i - 1] and ema_f[i] < ema_s[i]
            if pos == "long" and death:
                signals[i] = "close"; pos = None
            elif pos == "short" and golden:
                signals[i] = "close"; pos = None
            elif not pos and golden:
                signals[i] = "long"; pos = "long"
            elif not pos and death:
                signals[i] = "short"; pos = "short"
        return signals
    return fn


def make_rsi_signals(period=14, oversold=30, overbought=70):
    def fn(closes):
        rsi = calc_rsi(closes, period)
        signals = [None] * len(closes)
        pos = None
        for i in range(period + 1, len(closes)):
            if pos == "long" and rsi[i] > overbought:
                signals[i] = "close"; pos = None
            elif pos == "short" and rsi[i] < oversold:
                signals[i] = "close"; pos = None
            elif not pos and rsi[i - 1] < oversold and rsi[i] >= oversold:
                signals[i] = "long"; pos = "long"
            elif not pos and rsi[i - 1] > overbought and rsi[i] <= overbought:
                signals[i] = "short"; pos = "short"
        return signals
    return fn


def make_macd_signals(fast=12, slow=26, signal=9):
    def fn(closes):
        macd_line, signal_line, histogram = calc_macd(closes, fast, slow, signal)
        signals = [None] * len(closes)
        pos = None
        for i in range(slow + signal + 1, len(closes)):
            bull = histogram[i - 1] <= 0 and histogram[i] > 0
            bear = histogram[i - 1] >= 0 and histogram[i] < 0
            if pos == "long" and bear:
                signals[i] = "close"; pos = None
            elif pos == "short" and bull:
                signals[i] = "close"; pos = None
            elif not pos and bull:
                signals[i] = "long"; pos = "long"
            elif not pos and bear:
                signals[i] = "short"; pos = "short"
        return signals
    return fn


def make_bbands_signals(period=20, std_mult=2.0):
    def fn(closes):
        upper, mid, lower = calc_bbands(closes, period, std_mult)
        signals = [None] * len(closes)
        pos = None
        for i in range(period, len(closes)):
            if lower[i] is None:
                continue
            if pos == "long" and closes[i] >= mid[i]:
                signals[i] = "close"; pos = None
            elif pos == "short" and closes[i] <= mid[i]:
                signals[i] = "close"; pos = None
            elif not pos and closes[i] <= lower[i]:
                signals[i] = "long"; pos = "long"
            elif not pos and closes[i] >= upper[i]:
                signals[i] = "short"; pos = "short"
        return signals
    return fn


STRATEGIES = {
    "ema": (make_ema_signals, {"fast": 12, "slow": 26}),
    "rsi": (make_rsi_signals, {"period": 14, "oversold": 30, "overbought": 70}),
    "macd": (make_macd_signals, {"fast": 12, "slow": 26, "signal": 9}),
    "bbands": (make_bbands_signals, {"period": 20, "std_mult": 2.0}),
}


def run_backtest(candles, strategy: str, params: dict = None,
                 capital=1000, leverage=5, pos_pct=0.2,
                 sl_pct=0.03, tp_pct=0.06, fee_rate=0.0006) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy}. Available: {list(STRATEGIES.keys())}")
    
    maker, defaults = STRATEGIES[strategy]
    p = {**defaults, **(params or {})}
    signal_fn = maker(**p)
    result = _run_backtest(candles, signal_fn, capital, leverage, pos_pct, sl_pct, tp_pct, fee_rate, strategy, p)
    return result.summary()


def main():
    parser = argparse.ArgumentParser(description="Crypto Backtesting Engine")
    parser.add_argument("--symbol", default="ETH/USDT:USDT", help="Trading pair (ccxt format)")
    parser.add_argument("--exchange", default="bybit", help="Exchange (ccxt id)")
    parser.add_argument("--timeframe", default="1h", help="Candle timeframe")
    parser.add_argument("--limit", type=int, default=1000, help="Number of candles")
    parser.add_argument("--strategy", default="ema", choices=list(STRATEGIES.keys()))
    parser.add_argument("--capital", type=float, default=1000)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--pos-pct", type=float, default=0.2)
    parser.add_argument("--sl-pct", type=float, default=0.03)
    parser.add_argument("--tp-pct", type=float, default=0.06)
    parser.add_argument("--json-output", help="Save results to JSON file")
    # Strategy-specific params
    parser.add_argument("--fast", type=int, default=12)
    parser.add_argument("--slow", type=int, default=26)
    parser.add_argument("--period", type=int, default=14)
    parser.add_argument("--oversold", type=int, default=30)
    parser.add_argument("--overbought", type=int, default=70)
    parser.add_argument("--signal", type=int, default=9)
    parser.add_argument("--std-mult", type=float, default=2.0)

    args = parser.parse_args()

    print(f"Fetching {args.limit} candles for {args.symbol} from {args.exchange}...")
    candles = fetch_candles(args.symbol, args.timeframe, args.limit, args.exchange)
    print(f"Got {len(candles)} candles")

    # Build strategy params from CLI
    params = {}
    if args.strategy == "ema":
        params = {"fast": args.fast, "slow": args.slow}
    elif args.strategy == "rsi":
        params = {"period": args.period, "oversold": args.oversold, "overbought": args.overbought}
    elif args.strategy == "macd":
        params = {"fast": args.fast, "slow": args.slow, "signal": args.signal}
    elif args.strategy == "bbands":
        params = {"period": args.period, "std_mult": args.std_mult}

    result = run_backtest(candles, args.strategy, params,
                          args.capital, args.leverage, args.pos_pct, args.sl_pct, args.tp_pct)

    print(f"\n{'='*50}")
    print(f"Strategy: {result['strategy']} {result['params']}")
    print(f"Trades: {result['trades']} | Wins: {result['wins']} | Win Rate: {result['win_rate']}%")
    print(f"PnL: ${result['total_pnl']} ({result['pnl_pct']}%)")
    print(f"Max Drawdown: {result['max_drawdown_pct']}%")
    print(f"Profit Factor: {result['profit_factor']}")
    print(f"Best: ${result['best_trade']} | Worst: ${result['worst_trade']}")
    print(f"Balance: ${result['initial_capital']} → ${result['final_balance']}")
    print(f"{'='*50}")

    if args.json_output:
        with open(args.json_output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Saved to {args.json_output}")


if __name__ == "__main__":
    main()
