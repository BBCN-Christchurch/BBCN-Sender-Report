#!/usr/bin/env python3
"""
Sender.net Data Fetcher
----------------------
Pulls campaign performance and subscriber metrics from the Sender.net API
and logs them to local CSV files in the data/ directory.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_URL = "https://api.sender.net/v2"
DATE_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOOKBACK_DAYS = 30
ANNUAL_DAYS = 365


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

class SenderAPIError(RuntimeError):
    pass


class SenderClient:
    def __init__(self, token: str):
        if not token:
            raise SenderAPIError(
                "No API token found. Set SENDER_API_TOKEN as an environment "
                "variable (locally via .env, or as a GitHub Actions secret)."
            )
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        for attempt in range(5):
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5))
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 2 ** attempt
                print(f"  Server error {resp.status_code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise SenderAPIError(f"GET {path} failed: {resp.status_code} {resp.text[:300]}")
            return resp.json()
        raise SenderAPIError(f"GET {path} failed after retries")

    def paginate(self, path: str, params: dict | None = None, max_pages: int = 500):
        params = dict(params or {})
        page = 1
        while page <= max_pages:
            params["page"] = page
            payload = self._get(path, params=params)
            data = payload.get("data", [])
            if isinstance(data, dict):
                data = [data]
            meta = payload.get("meta", {})
            yield data, meta
            last_page = meta.get("last_page", page)
            if not data or page >= last_page:
                return
            page += 1

    def get_campaigns(self, status: str | None = None, max_pages: int = 20):
        params = {"limit": 100}
        if status:
            params["status"] = status
        campaigns = []
        for data, _ in self.paginate("/campaigns", params=params, max_pages=max_pages):
            campaigns.extend(data)
        return campaigns

    def get_campaign_detail(self, campaign_id: str) -> dict:
        payload = self._get(f"/campaigns/{campaign_id}")
        return payload.get("data", {})

    def get_unique_opens(self, campaign_id: str) -> int:
        seen = set()
        for data, _ in self.paginate(f"/campaigns/{campaign_id}/opens", max_pages=500):
            for row in data:
                rid = row.get("recipient_id") or row.get("email")
                if rid:
                    seen.add(rid)
        return len(seen)

    def get_all_subscribers(self, max_pages: int = 1000):
        subscribers = []
        total = None
        for data, meta in self.paginate("/subscribers", params={"limit": 1000}, max_pages=max_pages):
            subscribers.extend(data)
            if total is None:
                total = meta.get("total")
        return subscribers, total


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_dt(value):
    if not value:
        return None
    for fmt in (DATE_FMT, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def get_latest_sent_campaign(client: SenderClient):
    campaigns = client.get_campaigns(status="SENT")
    if not campaigns:
        return None
    campaigns.sort(key=lambda c: c.get("sent_time") or c.get("modified") or "", reverse=True)
    latest = campaigns[0]
    detail = client.get_campaign_detail(latest["id"])
    return detail or latest


def build_campaign_summary(client: SenderClient, campaign: dict) -> dict:
    sent_count = campaign.get("sent_count", 0) or 0
    recipient_count = campaign.get("recipient_count", 0) or 0
    bounces = campaign.get("bounces_count", 0) or 0
    delivered = max(sent_count - bounces, 0)

    unique_opens = campaign.get("opens", 0) or 0
    try:
        unique_opens = client.get_unique_opens(campaign["id"])
    except SenderAPIError as exc:
        print(f"  Warning: could not fetch unique opens ({exc}); using campaign totals field instead.", file=sys.stderr)

    open_rate = (unique_opens / delivered * 100) if delivered else 0
    bounce_rate = (bounces / sent_count * 100) if sent_count else 0

    return {
        "subject": campaign.get("subject") or campaign.get("title") or "(untitled campaign)",
        "sent_time": campaign.get("sent_time"),
        "recipient_count": recipient_count,
        "sent_count": sent_count,
        "delivered": delivered,
        "bounces": bounces,
        "unique_opens": unique_opens,
        "clicks": campaign.get("clicks", 0) or 0,
        "open_rate": round(open_rate, 1),
        "bounce_rate": round(bounce_rate, 1),
    }


def _count_window(subscribers, since: datetime):
    new_count = 0
    unsub_count = 0
    for sub in subscribers:
        created = parse_dt(sub.get("created"))
        if created and created >= since:
            new_count += 1

        status = sub.get("status", {})
        email_status = status.get("email") if isinstance(status, dict) else status
        unsub_at = parse_dt(sub.get("unsubscribed_at"))
        if email_status == "unsubscribed" and unsub_at and unsub_at >= since:
            unsub_count += 1
    return new_count, unsub_count


def build_subscriber_summary(client: SenderClient, period_since: datetime, annual_since: datetime) -> dict:
    subscribers, total = client.get_all_subscribers()
    if total is None:
        total = len(subscribers)

    period_new, period_unsub = _count_window(subscribers, period_since)
    annual_new, annual_unsub = _count_window(subscribers, annual_since)

    return {
        "total_subscribers": total,
        "period_new": period_new,
        "period_unsub": period_unsub,
        "period_net": period_new - period_unsub,
        "annual_new": annual_new,
        "annual_unsub": annual_unsub,
        "annual_net": annual_new - annual_unsub,
    }


# --------------------------------------------------------------------------
# CSV read/write
# --------------------------------------------------------------------------

def get_period_since_from_csv(csv_path: Path) -> datetime | None:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return None
    try:
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if rows:
                last_row = rows[-1]
                last_date_str = last_row.get("date")
                if last_date_str:
                    return parse_dt(last_date_str)
    except Exception as e:
        print(f"Warning: could not read last run date from {csv_path} ({e})", file=sys.stderr)
    return None


def save_campaign_to_csv(csv_path: Path, summary: dict, campaign_id: str):
    rows = []
    updated = False
    fieldnames = [
        "campaign_id", "subject", "sent_time", "recipient_count",
        "sent_count", "delivered", "bounces", "unique_opens",
        "clicks", "open_rate", "bounce_rate"
    ]
    
    if csv_path.exists() and csv_path.stat().st_size > 0:
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("campaign_id") == campaign_id:
                        row.update({
                            "subject": summary["subject"],
                            "sent_time": summary["sent_time"],
                            "recipient_count": str(summary["recipient_count"]),
                            "sent_count": str(summary["sent_count"]),
                            "delivered": str(summary["delivered"]),
                            "bounces": str(summary["bounces"]),
                            "unique_opens": str(summary["unique_opens"]),
                            "clicks": str(summary["clicks"]),
                            "open_rate": str(summary["open_rate"]),
                            "bounce_rate": str(summary["bounce_rate"])
                        })
                        updated = True
                    rows.append(row)
        except Exception as e:
            print(f"Warning: could not read {csv_path} ({e})", file=sys.stderr)

    new_row = {
        "campaign_id": campaign_id,
        "subject": summary["subject"],
        "sent_time": summary["sent_time"],
        "recipient_count": summary["recipient_count"],
        "sent_count": summary["sent_count"],
        "delivered": summary["delivered"],
        "bounces": summary["bounces"],
        "unique_opens": summary["unique_opens"],
        "clicks": summary["clicks"],
        "open_rate": summary["open_rate"],
        "bounce_rate": summary["bounce_rate"]
    }

    if not updated:
        rows.append(new_row)

    with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    
    if updated:
        print(f"Updated campaign {campaign_id} details in {csv_path}")
    else:
        print(f"Appended new campaign {campaign_id} to {csv_path}")


def save_subscribers_to_csv(csv_path: Path, summary: dict, date_str: str, period_since_str: str, annual_since_str: str):
    fieldnames = [
        "date", "total_subscribers", "period_since", "period_new",
        "period_unsub", "period_net", "annual_since", "annual_new",
        "annual_unsub", "annual_net"
    ]
    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    with open(csv_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "date": date_str,
            "total_subscribers": summary["total_subscribers"],
            "period_since": period_since_str,
            "period_new": summary["period_new"],
            "period_unsub": summary["period_unsub"],
            "period_net": summary["period_net"],
            "annual_since": annual_since_str,
            "annual_new": summary["annual_new"],
            "annual_unsub": summary["annual_unsub"],
            "annual_net": summary["annual_net"]
        })
    print(f"Appended subscriber data to {csv_path}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch Sender.net API data and store in CSV files.")
    parser.add_argument("--days", type=int, default=None,
                        help="Force the period lookback window in days, overriding last run date.")
    parser.add_argument("--annual-days", type=int, default=ANNUAL_DAYS,
                        help="Lookback window in days for the annual comparison (default 365).")
    parser.add_argument("--data-dir", type=str, default="data",
                        help="Directory to read/write CSV files.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    campaigns_file = data_dir / "campaigns.csv"
    subscribers_file = data_dir / "subscribers.csv"

    now = datetime.now(timezone.utc)
    annual_since = now - timedelta(days=args.annual_days)

    if args.days:
        period_since = now - timedelta(days=args.days)
    else:
        # Check last run date in subscribers.csv
        last_run = get_period_since_from_csv(subscribers_file)
        period_since = last_run or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))

    token = os.environ.get("SENDER_API_TOKEN", "").strip()
    client = SenderClient(token)

    print("Fetching latest sent campaign...")
    latest = get_latest_sent_campaign(client)
    if not latest:
        print("No sent campaigns found on this account.", file=sys.stderr)
        sys.exit(1)
    
    campaign_summary = build_campaign_summary(client, latest)
    save_campaign_to_csv(campaigns_file, campaign_summary, latest["id"])

    print(f"Fetching subscriber data (period since {period_since.date()}, annual since {annual_since.date()})...")
    subs_summary = build_subscriber_summary(client, period_since, annual_since)
    
    now_str = now.strftime(DATE_FMT)
    period_since_str = period_since.strftime(DATE_FMT)
    annual_since_str = annual_since.strftime(DATE_FMT)

    save_subscribers_to_csv(subscribers_file, subs_summary, now_str, period_since_str, annual_since_str)
    print("\nData fetch complete.")


if __name__ == "__main__":
    main()
