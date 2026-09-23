# Adding Custom Strategies

## Signal Function Interface

A strategy is a function that takes a list of closes and returns a list of signals:

```python
def my_signal_factory(**params):
    def fn(closes: list[float]) -> list[str | None]:
        signals = [None] * len(closes)
        # Your logic: set signals[i] = "long", "short", or "close"
        return signals
    return fn
```

## Register

Add to the `STRATEGIES` dict in `backtest_engine.py`:

```python
STRATEGIES["mystrat"] = (my_signal_factory, {"param1": default1, "param2": default2})
```

Add sweep configs in `sweep.py`:

```python
SWEEP_CONFIGS["mystrat"] = [
    {"param1": v1, "param2": v2}
    for v1 in [...]
    for v2 in [...]
]
```

## Signal Values

- `"long"` — open long position (only when flat)
- `"short"` — open short position (only when flat)
- `"close"` — close current position
- `None` — no action

The engine handles SL/TP automatically. Your signals only need to handle entry/exit logic.
