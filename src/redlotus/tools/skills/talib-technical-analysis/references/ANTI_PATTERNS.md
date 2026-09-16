# Anti-Patterns (Cross-Project)

Total: **15**

## finance-bp-109--ta-lib-python (8)

### `AP-TECHNICAL-ANALYSIS-001` — C FFI Type Mismatch with Non-float64 Arrays <sub>(high)</sub>

Passing non-float64 (NPY_DOUBLE) numpy arrays to TA-Lib C functions causes memory corruption or silent incorrect calculations. The C FFI layer expects precisely float64 precision, and type mismatches propagate undetected, producing wrong indicator values that may silently corrupt trading strategies. Root cause is not validating array dtype before the C function call.

### `AP-TECHNICAL-ANALYSIS-002` — Multidimensional Array Memory Access Violations <sub>(high)</sub>

Passing multidimensional numpy arrays to TA-Lib C functions causes segmentation faults and memory access violations due to incorrect stride calculations. The C layer assumes contiguous 1-dimensional memory layouts, and higher-dimensional inputs break its internal pointer arithmetic, leading to crashes or silent memory corruption.

### `AP-TECHNICAL-ANALYSIS-003` — Ignoring TA_RetCode Error Status from C Calls <sub>(high)</sub>

When TA-Lib C functions return non-zero TA_RetCode values (indicating errors like uninitialized library, invalid parameters, or out-of-range inputs), ignoring these codes silently propagates invalid computation results. This leads to incorrect technical indicator values feeding into trading strategies without any warning, potentially causing significant financial loss.

### `AP-TECHNICAL-ANALYSIS-004` — Mismatched Array Lengths in Multi-Input Functions <sub>(high)</sub>

When calculating indicators that require multiple input arrays (e.g., open, high, low, close, volume), providing arrays of different lengths causes out-of-bounds memory access. TA-Lib iterates assuming identical sizes, and length mismatches produce garbage values or segmentation faults, corrupting the entire indicator output.

### `AP-TECHNICAL-ANALYSIS-011` — Stale Cached Outputs Without Invalidation <sub>(medium)</sub>

Caching computed indicator outputs without invalidating when inputs, parameters, or input_names change causes stale results to be returned even when underlying data has changed. This produces incorrect indicator values that silently propagate into trading strategies, leading to wrong signals based on outdated calculations.

### `AP-TECHNICAL-ANALYSIS-012` — Concurrent Access Without Thread-Local State <sub>(high)</sub>

Using shared Function instances across multiple threads without thread-local storage causes race conditions where concurrent threads share state. This leads to data corruption, incorrect results, and non-deterministic indicator values when multiple threads compute indicators simultaneously on the same instance.

### `AP-TECHNICAL-ANALYSIS-013` — Using Python Lists Instead of NumPy Arrays for Stream Functions <sub>(medium)</sub>

Stream functions require numpy.ndarray inputs due to direct C API access via PyArray_TYPE() and PyArray_FLAGS(). Passing plain Python lists or other sequences causes runtime errors because the C layer cannot access the underlying C arrays. This breaks real-time indicator calculations that expect efficient numpy buffer access.

### `AP-TECHNICAL-ANALYSIS-014` — Library Not Initialized Before C Function Calls <sub>(high)</sub>

Calling TA-Lib C functions without prior library initialization returns TA_RetCode=1 (TA_LIB_NOT_INITIALIZE), causing all function calls to fail. This is a silent failure mode that produces no output indicators, breaking batch calculation pipelines unless the initialization step is explicitly performed before any function calls.

## finance-bp-122--ta-python (7)

### `AP-TECHNICAL-ANALYSIS-005` — Time-Series Index Reindexing Breaks Alignment <sub>(high)</sub>

Reindexing or resetting the DataFrame/Series index after computing technical indicators breaks temporal alignment with original price data and other features. This causes look-ahead bias, shifts indicator values to incorrect timestamps, and corrupts time-series datasets when used in backtesting or feature engineering pipelines.

### `AP-TECHNICAL-ANALYSIS-006` — NaN/Inf/Zero Propagation Corrupts Indicator Values <sub>(high)</sub>

Failing to clean input data of NaN, infinite values, or zero prices causes cascading corruption through rolling window calculations. Division-by-zero errors on zero prices produce NaN that propagates into all subsequent indicator values, corrupting entire datasets. Invalid values also cause incorrect boolean mask classifications when compared with np.inf directly.

### `AP-TECHNICAL-ANALYSIS-007` — EMA Smoothing Parameter Divergence from TA Standards <sub>(medium)</sub>

Using pandas adjust=True (the default) for ewm() when implementing EMA-based indicators produces Yahoo Finance variant smoothing instead of standard recursive exponential smoothing per technical analysis textbooks. This causes different signal thresholds and divergence from widely-accepted indicator calculations, leading to inconsistent trading signals.

### `AP-TECHNICAL-ANALYSIS-008` — False Claims: Indicator Calculation as Trading Signal <sub>(high)</sub>

Presenting technical indicator values as real-time trading signals or guaranteed future performance misleads users about the tool's capabilities. The library calculates historical indicators from OHLCV data; claiming these as trading signals leads to improper trading decisions. Backtest results also do not guarantee future performance due to look-ahead bias and market regime changes.

### `AP-TECHNICAL-ANALYSIS-009` — Functional vs OOP API Implementation Divergence <sub>(medium)</sub>

When both functional wrappers (e.g., rsi()) and OOP classes (e.g., RSIIndicator) are provided, diverging implementations produce different indicator values for the same inputs. This causes confusion, test failures, and breaks user code that expects consistent behavior across APIs. The functional wrapper must delegate to the class implementation to ensure equivalence.

### `AP-TECHNICAL-ANALYSIS-010` — Bollinger Bands Using Sample Std Deviation <sub>(medium)</sub>

Using pandas default ddof=1 (sample standard deviation) for Bollinger Bands produces wider bands than John Bollinger's original specification, which uses population standard deviation. This causes overestimation of volatility, incorrect trading signal thresholds, and divergence from the canonical indicator calculation that traders expect.

### `AP-TECHNICAL-ANALYSIS-015` — Stateful Wrapper Functions Leak State Across Calls <sub>(medium)</sub>

When functional wrapper functions retain internal state between calls, different input series contaminate each other's results through data leakage. This produces incorrect indicator values when the same wrapper function is called sequentially with different data, as cached state from previous calls affects new computations.
