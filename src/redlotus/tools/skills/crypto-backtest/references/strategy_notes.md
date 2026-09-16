# Strategy Notes

## EMA Crossover
- Best for trending markets; gets chopped in sideways/range
- Faster EMAs (5/20) = more trades, more noise
- Slower EMAs (20/50) = fewer trades, catches big moves
- ETH tends to trend better than BTC for EMA strategies

## RSI
- Mean reversion: buys oversold, sells overbought
- Works best in ranging markets (opposite of EMA)
- Tighter thresholds (25/75) = fewer but higher-quality entries
- Can combine with trend filter (only long above 200 SMA)

## MACD
- Histogram crossover is the signal (not MACD/signal line)
- Lags more than raw EMA crossover but filters noise
- Signal period (default 9) controls responsiveness
- Works well on 4h+ timeframes

## Bollinger Bands
- Touch lower band → long (mean reversion)
- Touch upper band → short (mean reversion)
- Exit at middle band (SMA)
- Wider bands (2.5 std) = fewer signals but higher confidence
- Fails hard in strong trends (price walks the band)

## General Tips
- No strategy works in all conditions
- Combine trend + mean reversion strategies for diversification
- Always run sweep.py to find optimal params for YOUR market period
- Past performance doesn't guarantee future results
- Use paper trading to validate before going live
