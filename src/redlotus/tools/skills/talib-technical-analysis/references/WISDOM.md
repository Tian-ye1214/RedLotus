# Cross-Project Wisdom

Total: **9**

## `CW-TECHNICAL-ANALYSIS-001` — Explicit Input Validation Before Computation
**From**: finance-bp-109--ta-lib-python, finance-bp-122--ta-python · **Applicable to**: technical-analysis

Both projects require rigorous pre-computation validation: dtype checking (float64 for C FFI, numeric for pandas), dimension checking (1D arrays for C layer), and length validation. This defensive pattern prevents silent failures and memory corruption. Apply this pattern whenever interfacing with external C libraries or computing indicators on potentially malformed input data.

## `CW-TECHNICAL-ANALYSIS-002` — Index Preservation Throughout Indicator Pipeline
**From**: finance-bp-109--ta-lib-python, finance-bp-122--ta-python · **Applicable to**: technical-analysis

Preserving the original DataFrame/Series index without reindexing or reset is critical for temporal alignment. When constructing output Series, use index=self._close.index to maintain alignment with price data. This prevents look-ahead bias and ensures downstream features correctly reference their corresponding timestamps.

## `CW-TECHNICAL-ANALYSIS-003` — Data Cleaning Before Indicator Computation
**From**: finance-bp-122--ta-python · **Applicable to**: technical-analysis

Indicators like RSI, MACD, and Bollinger Bands produce incorrect results when fed NaN, inf, or zero values. Remove rows with zero prices (to prevent division-by-zero), filter out infinite values using the exp(709) threshold as the maximum float64, and apply dropna to DataFrames before passing to indicator functions. This ensures clean propagation through rolling window calculations.

## `CW-TECHNICAL-ANALYSIS-004` — Error Code Propagation from C to Python Layer
**From**: finance-bp-109--ta-lib-python · **Applicable to**: technical-analysis

Always call _ta_check_success and raise exceptions on non-zero TA_RetCode return values from C function calls. This pattern ensures that errors like uninitialized library, invalid parameters, or out-of-range inputs propagate as proper Python exceptions instead of silently producing garbage values. Never ignore return codes from the underlying C library.

## `CW-TECHNICAL-ANALYSIS-005` — Thread-Local Storage for Concurrent Indicator Access
**From**: finance-bp-109--ta-lib-python · **Applicable to**: technical-analysis

When the same Function instance may be accessed from multiple threads, use thread-local storage to maintain isolated state per thread. This prevents race conditions, state corruption, and non-deterministic results when concurrent threads compute indicators simultaneously. The pattern is essential for any multi-threaded trading system or async processing pipeline.

## `CW-TECHNICAL-ANALYSIS-006` — Functional Wrapper Delegates to OOP Implementation
**From**: finance-bp-122--ta-python · **Applicable to**: technical-analysis

Functional wrapper functions like rsi() and ema_indicator() should instantiate the corresponding Indicator class and call its result method, not reimplement logic. This ensures OOP and functional APIs produce identical outputs. Any divergence causes test failures and breaks user code that switches between API styles. Validate equivalence in test suites.

## `CW-TECHNICAL-ANALYSIS-007` — Standard TA Textbook Parameters for EMA Calculations
**From**: finance-bp-122--ta-python · **Applicable to**: technical-analysis

When implementing EMA-based indicators, use adjust=False in pandas ewm() to match standard recursive exponential smoothing from technical analysis textbooks, not the Yahoo Finance variant. Also use ddof=0 for Bollinger Bands standard deviation per the original specification. Deviations produce different signal thresholds that diverge from what traders expect.

## `CW-TECHNICAL-ANALYSIS-008` — Cache Invalidation on Any Input Change
**From**: finance-bp-109--ta-lib-python · **Applicable to**: technical-analysis

Set outputs_valid flag to False whenever inputs, parameters, or input_names change. This pattern prevents returning stale cached outputs when underlying data or parameters have been modified. Implement proper cache invalidation to ensure computed indicators always reflect the current state.

## `CW-TECHNICAL-ANALYSIS-009` — Library Initialization Before First Use
**From**: finance-bp-109--ta-lib-python, finance-bp-122--ta-python · **Applicable to**: technical-analysis

Explicitly initialize the TA-Lib C library before any function calls. Without initialization, all function calls fail with TA_RetCode=1 (TA_LIB_NOT_INITIALIZE). This is a critical setup step that must be performed once before the indicator computation pipeline begins, typically at application startup or when first loading the library.
