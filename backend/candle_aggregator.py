from dataclasses import dataclass, asdict, field
from typing import Optional, Callable
from collections import deque

TIMEFRAME_SECONDS: dict[str, int] = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}

@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    confirmed: bool = False   # True once a real (non-synthetic) trade lands
    buy_volume: float = 0.0   # sum of sol_amount for buy trades
    sell_volume: float = 0.0  # sum of sol_amount for sell trades
    pool_sol: float = 0.0     # iter28: last trade's pool liquidity depth (SOL in curve)
    market_cap_usd: float = 0.0  # last trade's USD market cap

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("confirmed", None)   # don't send internal flag to frontend
        return d


class CandleAggregator:
    def __init__(self, timeframe: str = "1m", history_size: int = 60):
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unknown timeframe '{timeframe}'")
        self.timeframe = timeframe
        self.tf_seconds = TIMEFRAME_SECONDS[timeframe]
        self.current_candle: Optional[Candle] = None
        # Rolling history of completed candles for sniper use
        self._history: deque[Candle] = deque(maxlen=history_size)
        # Optional callback fired on candle close
        self.on_candle_close: Optional[Callable[[Candle], None]] = None

    def _bucket(self, ts: float) -> int:
        return int(ts // self.tf_seconds) * self.tf_seconds

    def get_last_n(self, n: int) -> list[Candle]:
        """Return the last N completed candles plus the current in-progress candle."""
        result = list(self._history)
        if self.current_candle is not None:
            result.append(self.current_candle)
        return result[-n:]

    def process_trade(
        self,
        price: float,
        volume: float,
        timestamp: float,
        synthetic: bool = False,
        is_buy: Optional[bool] = None,   # True=buy, False=sell, None=unknown
        pool_sol: float = 0.0,           # iter28: pool liquidity depth (SOL in curve)
        market_cap_usd: float = 0.0,     # USD market cap from the trade feed
    ):
        bucket = self._bucket(timestamp)

        if self.current_candle is None:
            buy_vol = volume if is_buy is True else 0.0
            sell_vol = volume if is_buy is False else 0.0
            self.current_candle = Candle(
                time=bucket, open=price, high=price, low=price, close=price,
                volume=volume, confirmed=not synthetic,
                buy_volume=buy_vol, sell_volume=sell_vol,
                pool_sol=pool_sol,
                market_cap_usd=market_cap_usd,
            )
            return self.current_candle, True

        if bucket != self.current_candle.time:
            if synthetic:
                price_moved = abs(price - self.current_candle.close) > 1e-15
                if not price_moved:
                    c = self.current_candle
                    c.close = price
                    return c, False

            # Archive the completed candle
            self._history.append(self.current_candle)
            closed_candle = self.current_candle
            if self.on_candle_close:
                try:
                    self.on_candle_close(closed_candle)
                except Exception:
                    pass

            buy_vol = volume if is_buy is True else 0.0
            sell_vol = volume if is_buy is False else 0.0
            self.current_candle = Candle(
                time=bucket, open=price, high=price, low=price, close=price,
                volume=volume, confirmed=not synthetic,
                buy_volume=buy_vol, sell_volume=sell_vol,
                pool_sol=pool_sol if pool_sol > 0 else closed_candle.pool_sol,
                market_cap_usd=market_cap_usd if market_cap_usd > 0 else closed_candle.market_cap_usd,
            )
            return self.current_candle, True

        c = self.current_candle
        if price > c.high: c.high = price
        if price < c.low:  c.low  = price
        c.close = price
        c.volume += volume
        if pool_sol > 0:
            c.pool_sol = pool_sol
        if market_cap_usd > 0:
            c.market_cap_usd = market_cap_usd
        if is_buy is True:
            c.buy_volume += volume
        elif is_buy is False:
            c.sell_volume += volume
        if not synthetic:
            c.confirmed = True
        return c, False