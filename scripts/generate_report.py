#!/usr/bin/env python3
"""
Sender.net Report Generator
----------------------------
Pulls the latest campaign performance and subscriber growth from the
Sender.net API and renders a single self-contained HTML page.

Designed to run two ways:

1. Locally, for testing/previewing (python scripts/generate_report.py --sample)
2. On a schedule via GitHub Actions, which calls this with a real token
   supplied as a repository secret (never committed to the repo) and
   commits the resulting docs/index.html, which GitHub Pages then serves.

No token is ever written into the generated HTML.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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


def load_last_run(state_file: Path):
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            return parse_dt(state.get("last_run"))
        except (json.JSONDecodeError, KeyError):
            return None
    return None


def save_last_run(state_file: Path, when: datetime):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_run": when.strftime(DATE_FMT)}, indent=2))


# --------------------------------------------------------------------------
# Data assembly
# --------------------------------------------------------------------------

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
# Sample data (no API calls -- for previewing the design)
# --------------------------------------------------------------------------

def sample_data():
    campaign = {
        "subject": "August Product Update & New Features",
        "sent_time": "2026-08-14 09:00:00",
        "recipient_count": 4820,
        "sent_count": 4791,
        "delivered": 4707,
        "bounces": 84,
        "unique_opens": 1962,
        "clicks": 431,
        "open_rate": 41.7,
        "bounce_rate": 1.8,
    }
    subs = {
        "total_subscribers": 6432,
        "period_new": 187,
        "period_unsub": 23,
        "period_net": 164,
        "annual_new": 2140,
        "annual_unsub": 410,
        "annual_net": 1730,
    }
    return campaign, subs


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

def fmt_num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def fmt_date(value) -> str:
    dt = parse_dt(value)
    return dt.strftime("%d %b %Y, %H:%M") if dt else "—"


def signed(n) -> str:
    return f"+{fmt_num(n)}" if n >= 0 else f"−{fmt_num(abs(n))}"


def bounce_commentary(rate: float) -> str:
    if rate < 2:
        return "within a healthy deliverability range"
    if rate < 5:
        return "slightly elevated and worth monitoring"
    return "above typical deliverability thresholds and warrants review"


def net_commentary(net: int) -> str:
    if net > 0:
        return "net growth"
    if net < 0:
        return "net decline"
    return "no net change"


def render_bar_chart(period_new, period_unsub, annual_new, annual_unsub) -> str:
    """Server-rendered grouped SVG bar chart, no client-side JS or chart library."""
    width, height = 640, 300
    margin = {"top": 24, "right": 24, "bottom": 52, "left": 56}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]

    values = [period_new, period_unsub, annual_new, annual_unsub]
    max_val = max(values) if max(values) > 0 else 1
    # Round the axis ceiling up to a tidy number for clean gridlines.
    magnitude = 10 ** (len(str(max_val)) - 1)
    axis_max = ((max_val // magnitude) + 1) * magnitude

    groups = [
        ("This period", period_new, period_unsub),
        ("Last 12 months", annual_new, annual_unsub),
    ]

    group_w = plot_w / len(groups)
    bar_w = group_w * 0.26
    gap = group_w * 0.08

    def y_for(v):
        return margin["top"] + plot_h - (v / axis_max * plot_h)

    # Gridlines + axis labels (4 bands)
    gridlines = []
    for i in range(5):
        val = axis_max * i / 4
        y = y_for(val)
        gridlines.append(
            f'<line x1="{margin["left"]}" y1="{y:.1f}" x2="{width - margin["right"]}" y2="{y:.1f}" '
            f'stroke="#DCE1E8" stroke-width="1" />'
            f'<text x="{margin["left"] - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#6B7688" font-family="Inter, sans-serif">{fmt_num(round(val))}</text>'
        )

    bars = []
    labels = []
    for gi, (label, new_v, unsub_v) in enumerate(groups):
        gx = margin["left"] + gi * group_w
        cx = gx + group_w / 2

        x_new = cx - gap / 2 - bar_w
        x_unsub = cx + gap / 2

        y_new = y_for(new_v)
        y_unsub = y_for(unsub_v)

        bars.append(
            f'<rect x="{x_new:.1f}" y="{y_new:.1f}" width="{bar_w:.1f}" '
            f'height="{margin["top"] + plot_h - y_new:.1f}" fill="#1F3A5F" rx="2" />'
            f'<text x="{x_new + bar_w/2:.1f}" y="{y_new - 8:.1f}" text-anchor="middle" '
            f'font-size="12" font-weight="600" fill="#1F3A5F" font-family="Inter, sans-serif">{fmt_num(new_v)}</text>'

            f'<rect x="{x_unsub:.1f}" y="{y_unsub:.1f}" width="{bar_w:.1f}" '
            f'height="{margin["top"] + plot_h - y_unsub:.1f}" fill="#8B3A3A" rx="2" />'
            f'<text x="{x_unsub + bar_w/2:.1f}" y="{y_unsub - 8:.1f}" text-anchor="middle" '
            f'font-size="12" font-weight="600" fill="#8B3A3A" font-family="Inter, sans-serif">{fmt_num(unsub_v)}</text>'
        )
        labels.append(
            f'<text x="{cx:.1f}" y="{height - margin["bottom"] + 26:.1f}" text-anchor="middle" '
            f'font-size="12.5" fill="#16233D" font-family="Source Serif 4, serif">{label}</text>'
        )

    baseline_y = margin["top"] + plot_h
    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="New subscribers versus unsubscribes, this period and last 12 months">
      {''.join(gridlines)}
      <line x1="{margin['left']}" y1="{baseline_y:.1f}" x2="{width - margin['right']}" y2="{baseline_y:.1f}" stroke="#16233D" stroke-width="1.4" />
      {''.join(bars)}
      {''.join(labels)}
    </svg>
    """


def data_table(rows) -> str:
    body = "\n".join(
        f'<tr><td>{label}</td><td class="num">{value}</td></tr>'
        for label, value in rows
    )
    return f'<table class="report-table"><tbody>{body}</tbody></table>'


def render_html(campaign: dict, subs: dict, period_since: datetime, annual_since: datetime, generated_at: datetime) -> str:
    period_label = period_since.strftime("%d %b %Y")
    annual_label = annual_since.strftime("%d %b %Y")
    generated_label = generated_at.strftime("%d %B %Y")
    generated_time = generated_at.strftime("%H:%M UTC")

    exec_bullets = [
        f"The subscriber base totals <b>{fmt_num(subs['total_subscribers'])}</b>, a {net_commentary(subs['annual_net'])} "
        f"of <b>{signed(subs['annual_net'])}</b> over the trailing twelve months "
        f"({fmt_num(subs['annual_new'])} joined, {fmt_num(subs['annual_unsub'])} unsubscribed).",

        f"In the current reporting period (since {period_label}), net subscriber change was "
        f"<b>{signed(subs['period_net'])}</b> ({fmt_num(subs['period_new'])} joined, "
        f"{fmt_num(subs['period_unsub'])} unsubscribed).",

        f"The most recent campaign, &ldquo;{campaign['subject']}&rdquo;, was sent to "
        f"<b>{fmt_num(campaign['sent_count'])}</b> recipients on {fmt_date(campaign['sent_time'])}, achieving a "
        f"<b>{campaign['open_rate']}%</b> unique open rate and a <b>{campaign['bounce_rate']}%</b> bounce rate, "
        f"{bounce_commentary(campaign['bounce_rate'])}.",
    ]

    campaign_rows = [
        ("Recipients", fmt_num(campaign["recipient_count"])),
        ("Sends", fmt_num(campaign["sent_count"])),
        ("Delivered", fmt_num(campaign["delivered"])),
        ("Bounces", f"{fmt_num(campaign['bounces'])} ({campaign['bounce_rate']}%)"),
        ("Unique opens", f"{fmt_num(campaign['unique_opens'])} ({campaign['open_rate']}%)"),
    ]

    subscriber_rows = [
        ("Total subscribers", fmt_num(subs["total_subscribers"])),
        (f"New — this period (since {period_label})", fmt_num(subs["period_new"])),
        (f"Unsubscribed — this period (since {period_label})", fmt_num(subs["period_unsub"])),
        ("Net change — this period", signed(subs["period_net"])),
        (f"New — last 12 months (since {annual_label})", fmt_num(subs["annual_new"])),
        (f"Unsubscribed — last 12 months (since {annual_label})", fmt_num(subs["annual_unsub"])),
        ("Net change — last 12 months", signed(subs["annual_net"])),
    ]

    chart_svg = render_bar_chart(subs["period_new"], subs["period_unsub"], subs["annual_new"], subs["annual_unsub"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Email Marketing Performance Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,500;8..60,600;8..60,700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper: #FFFFFF;
    --bg: #F4F5F7;
    --ink: #16233D;
    --ink-soft: #5B6578;
    --line: #DCE1E8;
    --navy: #1F3A5F;
    --navy-soft: #EAF0F6;
    --burgundy: #8B3A3A;
    --burgundy-soft: #F5E9E9;
    --gold: #B08D3E;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: 'Inter', sans-serif;
    font-size: 14.5px;
    -webkit-font-smoothing: antialiased;
  }}
  .page {{
    max-width: 860px;
    margin: 40px auto 80px;
    background: var(--paper);
    border: 1px solid var(--line);
    box-shadow: 0 1px 3px rgba(22,35,61,0.06);
  }}

  .masthead {{
    padding: 44px 56px 28px;
    border-bottom: 3px solid var(--navy);
  }}
  .masthead-label {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--gold);
    font-weight: 600;
    margin-bottom: 10px;
  }}
  .masthead h1 {{
    font-family: 'Source Serif 4', serif;
    font-weight: 600;
    font-size: 30px;
    margin: 0 0 6px;
    color: var(--ink);
  }}
  .masthead .sub {{
    font-size: 14px;
    color: var(--ink-soft);
    margin-bottom: 20px;
  }}
  .meta-row {{
    display: flex;
    gap: 40px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
  }}
  .meta-item .meta-label {{
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-soft);
    margin-bottom: 3px;
  }}
  .meta-item .meta-value {{
    font-size: 13.5px;
    font-weight: 600;
  }}

  .body {{ padding: 8px 56px 48px; }}

  .section {{ margin-top: 40px; }}
  .section-heading {{
    display: flex;
    align-items: baseline;
    gap: 10px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 10px;
    margin-bottom: 18px;
  }}
  .section-num {{
    font-family: 'Source Serif 4', serif;
    font-size: 15px;
    color: var(--gold);
    font-weight: 600;
  }}
  .section-heading h2 {{
    font-family: 'Source Serif 4', serif;
    font-size: 19px;
    font-weight: 600;
    margin: 0;
  }}

  .exec-summary {{
    background: var(--navy-soft);
    border-left: 3px solid var(--navy);
    padding: 20px 24px;
    margin-top: 40px;
  }}
  .exec-summary h2 {{
    font-family: 'Source Serif 4', serif;
    font-size: 17px;
    margin: 0 0 12px;
  }}
  .exec-summary ul {{
    margin: 0;
    padding-left: 18px;
  }}
  .exec-summary li {{
    margin-bottom: 10px;
    line-height: 1.55;
    color: var(--ink);
  }}
  .exec-summary li:last-child {{ margin-bottom: 0; }}

  .report-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13.5px;
  }}
  .report-table td {{
    padding: 10px 4px;
    border-bottom: 1px solid var(--line);
  }}
  .report-table tr:nth-child(even) td {{ background: #FAFBFC; }}
  .report-table td.num {{
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }}
  .campaign-subject {{
    font-family: 'Source Serif 4', serif;
    font-size: 16px;
    font-style: italic;
    color: var(--ink-soft);
    margin: -4px 0 16px;
  }}

  .chart-wrap {{ margin-top: 8px; }}
  .chart-wrap svg {{ width: 100%; height: auto; display: block; }}
  .chart-legend {{
    display: flex;
    gap: 24px;
    margin-top: 10px;
    font-size: 12px;
    color: var(--ink-soft);
  }}
  .chart-legend .sw {{
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 2px;
    margin-right: 6px;
    vertical-align: middle;
  }}
  .sw.new {{ background: var(--navy); }}
  .sw.unsub {{ background: var(--burgundy); }}

  footer.report-footer {{
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
    font-size: 11.5px;
    color: var(--ink-soft);
    display: flex;
    justify-content: space-between;
  }}

  @media (max-width: 640px) {{
    .masthead, .body {{ padding-left: 24px; padding-right: 24px; }}
    .meta-row {{ flex-direction: column; gap: 12px; }}
  }}
</style>
</head>
<body>
  <div class="page">

    <div class="masthead">
      <div class="masthead-label">Monthly Performance Report</div>
      <h1>Email Marketing Performance</h1>
      <div class="sub">Sender.net Account Summary</div>
      <div class="meta-row">
        <div class="meta-item">
          <div class="meta-label">Report date</div>
          <div class="meta-value">{generated_label}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Period covered</div>
          <div class="meta-value">{period_label} – {generated_label}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Generated</div>
          <div class="meta-value">{generated_time}</div>
        </div>
      </div>
    </div>

    <div class="body">

      <div class="exec-summary">
        <h2>Executive Summary</h2>
        <ul>
          {''.join(f'<li>{b}</li>' for b in exec_bullets)}
        </ul>
      </div>

      <div class="section">
        <div class="section-heading">
          <span class="section-num">1.</span>
          <h2>Campaign Performance</h2>
        </div>
        <div class="campaign-subject">&ldquo;{campaign['subject']}&rdquo; — sent {fmt_date(campaign['sent_time'])}</div>
        {data_table(campaign_rows)}
      </div>

      <div class="section">
        <div class="section-heading">
          <span class="section-num">2.</span>
          <h2>Subscriber Growth</h2>
        </div>
        {data_table(subscriber_rows)}

        <div class="chart-wrap">
          {chart_svg}
          <div class="chart-legend">
            <span><i class="sw new"></i>New subscribers</span>
            <span><i class="sw unsub"></i>Unsubscribed</span>
          </div>
        </div>
      </div>

      <footer class="report-footer">
        <span>Prepared automatically from the Sender.net API</span>
        <span>No individual subscriber data included</span>
      </footer>

    </div>
  </div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a Sender.net performance report page.")
    parser.add_argument("--days", type=int, default=None,
                         help="Force the period lookback window in days, overriding since-last-run.")
    parser.add_argument("--annual-days", type=int, default=ANNUAL_DAYS,
                         help="Lookback window in days for the annual comparison (default 365).")
    parser.add_argument("--output-dir", type=str, default="docs",
                         help="Directory to write index.html into (docs/ is the GitHub Pages convention).")
    parser.add_argument("--state-dir", type=str, default=".state",
                         help="Directory used to remember the last run date between scheduled runs.")
    parser.add_argument("--sample", action="store_true",
                         help="Generate the page from sample data, without calling the API.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_file = Path(args.state_dir) / "last_run.json"

    now = datetime.now(timezone.utc)
    annual_since = now - timedelta(days=args.annual_days)

    if args.days:
        period_since = now - timedelta(days=args.days)
    else:
        period_since = load_last_run(state_file) or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))

    if args.sample:
        print("Generating page from SAMPLE data (no API calls made)...")
        campaign_summary, subs_summary = sample_data()
    else:
        token = os.environ.get("SENDER_API_TOKEN", "").strip()
        client = SenderClient(token)

        print("Fetching latest sent campaign...")
        latest = get_latest_sent_campaign(client)
        if not latest:
            print("No sent campaigns found on this account.", file=sys.stderr)
            sys.exit(1)
        campaign_summary = build_campaign_summary(client, latest)

        print(f"Fetching subscriber data (period since {period_since.date()}, annual since {annual_since.date()})...")
        subs_summary = build_subscriber_summary(client, period_since, annual_since)

    html = render_html(campaign_summary, subs_summary, period_since, annual_since, now)

    out_path = output_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")

    if not args.sample:
        save_last_run(state_file, now)

    print(f"\nPage written to: {out_path}")


if __name__ == "__main__":
    main()
