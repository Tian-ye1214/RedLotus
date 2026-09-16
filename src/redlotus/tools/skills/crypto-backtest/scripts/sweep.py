#!/usr/bin/env python3
"""
Multi-Strategy Parameter Sweep
Tests multiple strategies × parameter combos, ranks by PnL.
"""

import argparse
import json

from backtest_engine import fetch_candles, run_backtest


SWEEP_CONFIGS = {
    "ema": [
        {"fast": f, "slow": s} for f in [5, 8, 12, 20] for s in [20, 26, 50] if f < s
    ],
    "rsi": [
        {"period": p, "oversold": lo, "overbought": hi}
        for p in [7, 14, 21]
        for lo, hi in [(20, 80), (25, 75), (30, 70)]
    ],
    "macd": [{"fast": 12, "slow": 26, "signal": s} for s in [5, 9, 14]],
    "bbands": [
        {"period": p, "std_mult": m} for p in [10, 20, 30] for m in [1.5, 2.0, 2.5]
    ],
}


def main():
    parser = argparse.ArgumentParser(description="Multi-Strategy Sweep")
    parser.add_argument("--symbol", default="ETH/USDT:USDT")
    parser.add_argument("--exchange", default="bybit")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--strategies", default="ema,rsi,macd,bbands", help="Comma-separated"
    )
    parser.add_argument("--capital", type=float, default=1000)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--output", default="sweep_results.json")
    args = parser.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",")]

    print(f"Fetching {args.limit} candles for {args.symbol}...")
    candles = fetch_candles(args.symbol, args.timeframe, args.limit, args.exchange)
    print(f"Got {len(candles)} candles\n")

    results = []
    total = sum(len(SWEEP_CONFIGS.get(s, [{}])) for s in strategies)
    done = 0

    for strat in strategies:
        configs = SWEEP_CONFIGS.get(strat, [{}])
        for params in configs:
            done += 1
            try:
                result = run_backtest(
                    candles, strat, params, args.capital, args.leverage
                )
                result["symbol"] = args.symbol
                results.append(result)
                pnl = result["total_pnl"]
                emoji = "✅" if pnl > 0 else "❌"
                print(
                    f"[{done}/{total}] {emoji} {strat} {params} → ${pnl:+.2f} ({result['win_rate']}% WR)"
                )
            except Exception as e:
                print(f"[{done}/{total}] ⚠️ {strat} {params} → ERROR: {e}")

    # Sort by PnL
    results.sort(key=lambda r: r["total_pnl"], reverse=True)

    print(f"\n{'=' * 60}")
    print(f"TOP 5 STRATEGIES for {args.symbol}")
    print(f"{'=' * 60}")
    for i, r in enumerate(results[:5], 1):
        print(f"{i}. {r['strategy']} {r['params']}")
        print(
            f"   PnL: ${r['total_pnl']:+.2f} | WR: {r['win_rate']}% | DD: {r['max_drawdown_pct']}% | Trades: {r['trades']}"
        )

    print(f"\n{'=' * 60}")
    print("BOTTOM 3 (worst)")
    print(f"{'=' * 60}")
    for r in results[-3:]:
        print(f"   {r['strategy']} {r['params']} → ${r['total_pnl']:+.2f}")

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nAll {len(results)} results saved to {args.output}")


if __name__ == "__main__":
    main()
