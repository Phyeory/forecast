from dataclasses import dataclass, asdict
from typing import Optional

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

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("confirmed", None)   # don't send internal flag to frontend
        return d


class CandleAggregator:
    def __init__(self, timeframe: str = "1m"):
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unknown timeframe '{timeframe}'")
        self.timeframe = timeframe
        self.tf_seconds = TIMEFRAME_SECONDS[timeframe]
        self.current_candle: Optional[Candle] = None

    def _bucket(self, ts: float) -> int:
        return int(ts // self.tf_seconds) * self.tf_seconds

    def process_trade(
        self,
        price: float,
        volume: float,
        timestamp: float,
        synthetic: bool = False,   # <-- NEW
    ):
        bucket = self._bucket(timestamp)

        if self.current_candle is None:
            self.current_candle = Candle(
                time=bucket, open=price, high=price, low=price, close=price,
                volume=volume, confirmed=not synthetic,
            )
            return self.current_candle, True

        if bucket != self.current_candle.time:
            if synthetic:
                # Synthetic tick in a new time bucket:
                #   • price MOVED  → open new candle (real price change)
                #   • price FLAT   → extend current candle (avoid ghost candles)
                price_moved = abs(price - self.current_candle.close) > 1e-15
                if not price_moved:
                    c = self.current_candle
                    c.close = price
                    return c, False

            self.current_candle = Candle(
                time=bucket, open=price, high=price, low=price, close=price,
                volume=volume, confirmed=not synthetic,
            )
            return self.current_candle, True

        c = self.current_candle
        if price > c.high: c.high = price
        if price < c.low:  c.low  = price
        c.close = price
        c.volume += volume
        if not synthetic:
            c.confirmed = True
        return c, False