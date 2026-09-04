"""Compare order-system campaigns with Google Ads journey attribution.

The module supports local CSVs for development and BigQuery for production.
BigQuery credentials are supplied through Application Default Credentials; no
secrets are stored in code.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class AttributionMismatch:
    order_id: str
    order_campaign_id: str | None
    google_campaign_id: str | None
    touchpoint_count: int
    first_touch_campaign_id: str | None
    last_touch_campaign_id: str | None
    conversion_time: str | None


def normalize_campaign_id(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    normalized = str(value).strip()
    return normalized or None


def _time(value: object) -> pd.Timestamp:
    return pd.to_datetime(value, utc=True, errors="coerce")


def expected_campaigns(
    touchpoints: pd.DataFrame,
    rule: str = "last_click",
) -> pd.DataFrame:
    """Return one Google Ads campaign per order using a deterministic rule."""
    required = {"order_id", "campaign_id", "touchpoint_time"}
    missing = required - set(touchpoints.columns)
    if missing:
        raise ValueError(f"touchpoints missing columns: {sorted(missing)}")
    data = touchpoints.copy()
    data["order_id"] = data["order_id"].astype(str)
    data["campaign_id"] = data["campaign_id"].map(normalize_campaign_id)
    data["touchpoint_time"] = data["touchpoint_time"].map(_time)
    data = data.dropna(subset=["order_id", "touchpoint_time"])
    data = data.sort_values(["order_id", "touchpoint_time"])
    grouped = data.groupby("order_id", as_index=False)
    result = grouped.agg(
        first_touch_campaign_id=("campaign_id", "first"),
        last_touch_campaign_id=("campaign_id", "last"),
        touchpoint_count=("campaign_id", "size"),
        conversion_time=("touchpoint_time", "max"),
    )
    if rule == "last_click":
        result["google_campaign_id"] = result["last_touch_campaign_id"]
    elif rule == "first_click":
        result["google_campaign_id"] = result["first_touch_campaign_id"]
    else:
        raise ValueError("rule must be 'last_click' or 'first_click'")
    return result


def find_mismatches(
    orders: pd.DataFrame,
    touchpoints: pd.DataFrame,
    rule: str = "last_click",
) -> pd.DataFrame:
    """Find orders whose stored campaign differs from Google Ads attribution."""
    required = {"order_id", "order_campaign_id"}
    missing = required - set(orders.columns)
    if missing:
        raise ValueError(f"orders missing columns: {sorted(missing)}")
    order_data = orders.copy()
    order_data["order_id"] = order_data["order_id"].astype(str)
    order_data["order_campaign_id"] = order_data["order_campaign_id"].map(normalize_campaign_id)
    attributed = expected_campaigns(touchpoints, rule)
    compared = order_data.merge(attributed, on="order_id", how="inner")
    mismatches = compared[
        compared["order_campaign_id"] != compared["google_campaign_id"]
    ].copy()
    return mismatches[
        [
            "order_id",
            "order_campaign_id",
            "google_campaign_id",
            "touchpoint_count",
            "first_touch_campaign_id",
            "last_touch_campaign_id",
            "conversion_time",
        ]
    ].rename(columns={"order_campaign_id": "order_campaign_id_system"})


def read_bigquery(project: str, query: str) -> pd.DataFrame:
    """Execute a parameter-free query using Google Application Default Credentials."""
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    return client.query(query).result().to_dataframe()


def run(orders: pd.DataFrame, touchpoints: pd.DataFrame, rule: str) -> pd.DataFrame:
    mismatches = find_mismatches(orders, touchpoints, rule)
    return mismatches


def main() -> None:
    parser = argparse.ArgumentParser(description="Find campaign attribution mismatches.")
    parser.add_argument("--orders", type=Path, required=True, help="CSV with order_id, order_campaign_id")
    parser.add_argument("--touchpoints", type=Path, required=True, help="CSV with order_id, campaign_id, touchpoint_time")
    parser.add_argument("--output", type=Path, default=Path("attribution_mismatches.csv"))
    parser.add_argument("--rule", choices=["last_click", "first_click"], default="last_click")
    args = parser.parse_args()
    mismatches = run(pd.read_csv(args.orders), pd.read_csv(args.touchpoints), args.rule)
    mismatches.to_csv(args.output, index=False)
    print(mismatches.to_string(index=False))
    print(f"\nFound {len(mismatches)} mismatch(es); saved to {args.output}")


if __name__ == "__main__":
    main()
