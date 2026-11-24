"""Fetch current week's high-impact (Red Folder) news from ForexFactory JSON feed."""

import argparse
from pathlib import Path
import pandas as pd
import requests

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Download this week's ForexFactory red-folder news")
    parser.add_argument("--out", type=Path, default=Path("ff_news_thisweek.csv"))
    args = parser.parse_args()

    resp = requests.get(FF_URL, timeout=15)
    resp.raise_for_status()
    events = resp.json()

    rows = []
    for ev in events:
        if str(ev.get("impact", "")).lower() != "high":
            continue
        rows.append(
            {
                "timestamp": ev.get("date"),
                "currency": str(ev.get("country", "")).upper(),
                "title": ev.get("title", "High Impact News"),
                "impact": "HIGH",
            }
        )

    df = pd.DataFrame(rows)
    df.to_csv(args.out, index=False)
    print(f"Saved {len(df)} high-impact events to {args.out}")


if __name__ == "__main__":
    main()
