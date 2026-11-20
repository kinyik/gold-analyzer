"""在终端看实时金价在跳：python watch_price.py   （Ctrl+C 结束）"""
from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.data_sources import build_source


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    source = build_source(settings)
    source.connect()
    print(f"数据源：{source.name}　品种：{settings.mt5_symbol}　（Ctrl+C 结束）")
    last_bid = None
    try:
        while True:
            try:
                tick = source.get_tick()
            except ConnectionError as exc:
                print(f"取价失败：{exc}，5 秒后重试")
                time.sleep(5)
                continue
            arrow = " "
            if last_bid is not None:
                arrow = "▲" if tick.bid > last_bid else "▼" if tick.bid < last_bid else "="
            last_bid = tick.bid
            print(
                f"{tick.time:%Y-%m-%d %H:%M:%S} UTC  {arrow} bid {tick.bid:.2f}  "
                f"ask {tick.ask:.2f}  点差 {tick.spread:.2f}"
            )
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        source.close()


if __name__ == "__main__":
    main()
