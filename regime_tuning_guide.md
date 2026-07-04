# Regime Sensitivity Tuning Guide

If you want the `StrategyEngine` to react to regime changes (e.g., `TREND` -> `EXHAUSTION` -> `REVERSAL` or `CONTINUATION`) more sensitively, you should tune the following parameters in `backend/strategy_engine.py`:

1. **`confidence_high`** (currently `0.79`):
   * **Why:** This is the primary "gatekeeper" for regime transitions. If `trend_confidence < confidence_high`, the engine aborts and basically stays frozen in its current state (the "ambiguous zone").
   * **Action:** Lower it. The comment above it mentions it was tuned "from 0.60 to 0.62", but it's currently at `0.79`, which makes the engine *extremely* insensitive. Try dropping it down to `0.60 - 0.65`.

2. **`regime_lookback`** (currently `6`):
   * **Why:** This dictates the rolling window length used to calculate persistence, momentum strength, and volatility expansion. 
   * **Action:** Lower it to `3` or `4`. A shorter memory means the regime filters will react much quicker to a sudden shift in price action.

3. **`kalman_gamma`** (currently `0.1`):
   * **Why:** Controls the learning rate of the `KalmanFilterMomentum` which estimates `m_hat` (momentum). 
   * **Action:** Increase it (e.g., `0.15` to `0.25`). Higher gamma makes the momentum estimation snap faster to new prices rather than lagging behind them.

4. **`persistence_threshold`** (currently `2`):
   * **Why:** Dictates how many consecutive bars the momentum must hold its sign to be considered a trend.
   * **Action:** Lower it to `1`. This will allow even a single strong bar of momentum to trigger trend conditions.

5. **`exhaustion_persist_bars`** (currently `4`) / **`reversal_confirm_bars`** (currently `2`):
   * **Why:** These add enforced physical delay (in bars) before the code formally declares an `EXHAUSTION` or `REVERSAL` state.
   * **Action:** Lower them. E.g. `exhaustion_persist_bars` -> `2` and `reversal_confirm_bars` -> `1`.
