"""Download 5m BTCUSDT historical klines from Binance Vision into data/BTCUSD.csv."""

import io
import zipfile
from pathlib import Path
from typing import List, Tuple
import pandas as pd
import requests

BASE_URL = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/5m"


def download_months(months: List[Tuple[int, int]], out_path: Path) -> None:
    frames = []
    for year, month in months:
        url = f"{BASE_URL}/BTCUSDT-5m-{year}-{month:02d}.zip"
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"[WARN] {url} -> HTTP {resp.status_code}")
            continue
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                df = pd.read_csv(f, header=None)
        # Binance kline cols: 0=open_time, 1=open, 2=high, 3=low, 4=close
        ts_raw = df[0].astype("int64")
        # Handle both milliseconds (13 digits) and microseconds (16 digits, 2025+)
        unit = "us" if ts_raw.iloc[0] > 10**14 else "ms"
        sub = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(ts_raw, unit=unit, utc=True),
                "open": df[1].astype(float),
                "high": df[2].astype(float),
                "low": df[3].astype(float),
                "close": df[4].astype(float),
            }
        )
        frames.append(sub)
        print(f"[OK] BTCUSDT {year}-{month:02d}: {len(sub)} bars")

    if not frames:
        raise RuntimeError("No BTCUSDT data downloaded.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    combined.to_csv(out_path, index=False)
    print(f"Saved {len(combined)} total bars -> {out_path}")


def main() -> None:
    months_2025 = [(2024, 12)] + [(2025, m) for m in range(1, 13)]
    download_months(months_2025, Path("data/BTCUSD.csv"))

    months_2026 = [(2025, 12)] + [(2026, m) for m in range(1, 11)]
    download_months(months_2026, Path("data_2026/BTCUSD.csv"))


if __name__ == "__main__":
    main()
