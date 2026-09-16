# Semantic Locks + Preconditions

## Semantic Locks (12)

### `SL-01` <sub>(on_violation: fatal)</sub>
Execute sell orders before buy orders in every trading cycle

### `SL-02` <sub>(on_violation: fatal)</sub>
Trading signals MUST use next-bar execution (no look-ahead)

### `SL-03` <sub>(on_violation: fatal)</sub>
Entity IDs MUST follow format entity_type_exchange_code

### `SL-04` <sub>(on_violation: fatal)</sub>
DataFrame index MUST be MultiIndex (entity_id, timestamp)

### `SL-05` <sub>(on_violation: fatal)</sub>
TradingSignal MUST have EXACTLY ONE of: position_pct, order_money, order_amount

### `SL-06` <sub>(on_violation: fatal)</sub>
filter_result column semantics: True=BUY, False=SELL, None/NaN=NO ACTION

### `SL-07` <sub>(on_violation: fatal)</sub>
Transformer MUST run BEFORE Accumulator in factor pipeline

### `SL-08` <sub>(on_violation: fatal)</sub>
MACD parameters locked: fast=12, slow=26, signal=9

### `SL-09` <sub>(on_violation: warning)</sub>
Default transaction costs: buy_cost=0.001, sell_cost=0.001, slippage=0.001

### `SL-10` <sub>(on_violation: fatal)</sub>
A-share equity trading is T+1 (no same-day close of buy positions)

### `SL-11` <sub>(on_violation: fatal)</sub>
Recorder subclass MUST define provider AND data_schema class attributes

### `SL-12` <sub>(on_violation: fatal)</sub>
Factor result_df MUST contain either 'filter_result' OR 'score_result' column

## Preconditions (4)

- **`PC-01`**: `python3 -c 'import zvt; print(zvt.__version__)'` → on_fail: Run: python3 -m pip install zvt then re-run: python3 -m zvt.init_dirs to initialize data directories
- **`PC-02`**: `python3 -c "from zvt.api.kdata import get_kdata; df = get_kdata(entity_ids=['stock_sh_600000'], limit=1); assert df is not None and len(df) > 0, 'No kdata found'"` → on_fail: Run recorder first: python3 -m zvt.recorders.em.em_stock_kdata_recorder --entity_ids stock_sh_600000 (replace with your target entity IDs)
- **`PC-03`**: `python3 -c "import os; from pathlib import Path; zvt_home = Path(os.environ.get('ZVT_HOME', Path.home() / '.zvt')); assert zvt_home.exists(), f'ZVT home not found: {zvt_home}'"` → on_fail: Run: python3 -m zvt.init_dirs
- **`PC-04`**: `python3 -c "import os, tempfile; from pathlib import Path; zvt_home = Path(os.environ.get('ZVT_HOME', Path.home() / '.zvt')); test_f = zvt_home / '.write_test'; test_f.touch(); test_f.unlink()"` → on_fail: Check directory permissions: chmod u+w ~/.zvt or set ZVT_HOME environment variable to a writable location
