#!/usr/bin/env python3
"""
Sender.net Report Generator
----------------------------
Reads campaign performance and subscriber growth logs from local CSV files
and renders them into a single self-contained HTML page (docs/index.html).
"""

import argparse
import csv
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATE_FMT = "%Y-%m-%d %H:%M:%S"
ANNUAL_DAYS = 365


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


# --------------------------------------------------------------------------
# CSV loaders
# --------------------------------------------------------------------------

def load_latest_campaign(csv_path: Path) -> dict | None:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return None
    try:
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if rows:
                latest = rows[-1]
                return {
                    "subject": latest.get("subject", "(untitled campaign)"),
                    "sent_time": latest.get("sent_time"),
                    "recipient_count": int(latest.get("recipient_count", 0)),
                    "sent_count": int(latest.get("sent_count", 0)),
                    "delivered": int(latest.get("delivered", 0)),
                    "bounces": int(latest.get("bounces", 0)),
                    "unique_opens": int(latest.get("unique_opens", 0)),
                    "clicks": int(latest.get("clicks", 0)),
                    "open_rate": float(latest.get("open_rate", 0)),
                    "bounce_rate": float(latest.get("bounce_rate", 0))
                }
    except Exception as e:
        print(f"Error reading campaign from CSV: {e}", file=sys.stderr)
    return None


def load_campaigns_in_period(csv_path: Path, since: datetime) -> list[dict]:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return []
    campaigns = []
    try:
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sent_time = parse_dt(row.get("sent_time"))
                if sent_time and sent_time >= since:
                    campaigns.append({
                        "subject": row.get("subject", "(untitled campaign)"),
                        "sent_time": row.get("sent_time"),
                        "recipient_count": int(row.get("recipient_count", 0)),
                        "sent_count": int(row.get("sent_count", 0)),
                        "delivered": int(row.get("delivered", 0)),
                        "bounces": int(row.get("bounces", 0)),
                        "unique_opens": int(row.get("unique_opens", 0)),
                        "clicks": int(row.get("clicks", 0)),
                        "open_rate": float(row.get("open_rate", 0)),
                        "bounce_rate": float(row.get("bounce_rate", 0))
                    })
    except Exception as e:
        print(f"Error reading campaigns from CSV: {e}", file=sys.stderr)
    
    # Sort descending by sent_time
    campaigns.sort(key=lambda c: c["sent_time"] or "", reverse=True)
    return campaigns


def load_latest_subscribers(csv_path: Path) -> tuple[dict, datetime, datetime] | None:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return None
    try:
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if rows:
                latest = rows[-1]
                subs = {
                    "total_subscribers": int(latest.get("total_subscribers", 0)),
                    "period_new": int(latest.get("period_new", 0)),
                    "period_unsub": int(latest.get("period_unsub", 0)),
                    "period_net": int(latest.get("period_net", 0)),
                    "annual_new": int(latest.get("annual_new", 0)),
                    "annual_unsub": int(latest.get("annual_unsub", 0)),
                    "annual_net": int(latest.get("annual_net", 0))
                }
                period_since = parse_dt(latest.get("period_since")) or (datetime.now(timezone.utc) - timedelta(days=30))
                annual_since = parse_dt(latest.get("annual_since")) or (datetime.now(timezone.utc) - timedelta(days=365))
                return subs, period_since, annual_since
    except Exception as e:
        print(f"Error reading subscribers from CSV: {e}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Sample data (no CSV -- fallback for previewing the design)
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

def render_bar_chart(period_new, period_unsub, annual_new, annual_unsub, period_since: datetime, annual_since: datetime) -> str:
    """Server-rendered horizontal stacked bar chart."""
    period_total = period_new + period_unsub
    if period_total > 0:
        period_new_pct = (period_new / period_total) * 100
        period_unsub_pct = (period_unsub / period_total) * 100
    else:
        period_new_pct = 0
        period_unsub_pct = 0

    annual_total = annual_new + annual_unsub
    if annual_total > 0:
        annual_new_pct = (annual_new / annual_total) * 100
        annual_unsub_pct = (annual_unsub / annual_total) * 100
    else:
        annual_new_pct = 0
        annual_unsub_pct = 0

    return f"""
    <div class="hbar-row">
      <div class="hbar-head">
        <span class="hbar-label">This period</span>
        <span class="hbar-range">since {period_since.strftime("%d %b %Y")}</span>
      </div>
      <div class="hbar-track">
        <div class="hbar-seg new" style="width:{period_new_pct:.1f}%"></div>
        <div class="hbar-seg unsub" style="width:{period_unsub_pct:.1f}%"></div>
      </div>
      <div class="hbar-legend">
        <span><i class="sw new"></i>New {fmt_num(period_new)}</span>
        <span><i class="sw unsub"></i>Unsubscribed {fmt_num(period_unsub)}</span>
      </div>
    </div>
    
    <div class="hbar-row">
      <div class="hbar-head">
        <span class="hbar-label">Last 12 months</span>
        <span class="hbar-range">since {annual_since.strftime("%d %b %Y")}</span>
      </div>
      <div class="hbar-track">
        <div class="hbar-seg new" style="width:{annual_new_pct:.1f}%"></div>
        <div class="hbar-seg unsub" style="width:{annual_unsub_pct:.1f}%"></div>
      </div>
      <div class="hbar-legend">
        <span><i class="sw new"></i>New {fmt_num(annual_new)}</span>
        <span><i class="sw unsub"></i>Unsubscribed {fmt_num(annual_unsub)}</span>
      </div>
    </div>
    """


def data_table(rows) -> str:
    body = "\n".join(
        f'<tr><td>{label}</td><td class="num">{value}</td></tr>'
        for label, value in rows
    )
    return f'<table class="report-table"><tbody>{body}</tbody></table>'


def render_html(campaigns: list[dict], subs: dict, period_since: datetime, annual_since: datetime, generated_at: datetime) -> str:
    period_label = period_since.strftime("%d %b %Y")
    annual_label = annual_since.strftime("%d %b %Y")
    generated_label = generated_at.strftime("%d %B %Y")
    generated_time = generated_at.strftime("%H:%M UTC")

    campaign_bullet = ""
    if not campaigns:
        campaign_bullet = "No campaigns were sent during this period."
    elif len(campaigns) == 1:
        campaign_bullet = (
            f"The campaign, &ldquo;{campaigns[0]['subject']}&rdquo;, was sent to "
            f"<b>{fmt_num(campaigns[0]['sent_count'])}</b> recipients on {fmt_date(campaigns[0]['sent_time'])}, achieving a "
            f"<b>{campaigns[0]['open_rate']}%</b> unique open rate and a <b>{campaigns[0]['bounce_rate']}%</b> bounce rate, "
            f"{bounce_commentary(campaigns[0]['bounce_rate'])}."
        )
    else:
        total_sends = sum(c['sent_count'] for c in campaigns)
        avg_open = round(sum(c['open_rate'] for c in campaigns) / len(campaigns), 1)
        avg_bounce = round(sum(c['bounce_rate'] for c in campaigns) / len(campaigns), 1)
        campaign_bullet = (
            f"During this period, <b>{len(campaigns)}</b> campaigns were sent, reaching a total of "
            f"<b>{fmt_num(total_sends)}</b> subscribers. The average unique open rate "
            f"was <b>{avg_open}%</b>, with an average bounce rate of <b>{avg_bounce}%</b>."
        )

    exec_bullets = [
        f"The subscriber base totals <b>{fmt_num(subs['total_subscribers'])}</b>, a {net_commentary(subs['annual_net'])} "
        f"of <b>{signed(subs['annual_net'])}</b> over the trailing twelve months "
        f"({fmt_num(subs['annual_new'])} joined, {fmt_num(subs['annual_unsub'])} unsubscribed).",

        f"In the current reporting period (since {period_label}), net subscriber change was "
        f"<b>{signed(subs['period_net'])}</b> ({fmt_num(subs['period_new'])} joined, "
        f"{fmt_num(subs['period_unsub'])} unsubscribed).",

        campaign_bullet,
    ]

    campaigns_blocks = []
    if not campaigns:
        campaigns_blocks.append('<div class="campaign-item"><p>No campaigns sent during this period.</p></div>')
    else:
        for i, c in enumerate(campaigns):
            campaign_rows = [
                ("Recipients", fmt_num(c["recipient_count"])),
                ("Sends", fmt_num(c["sent_count"])),
                ("Delivered", fmt_num(c["delivered"])),
                ("Bounces", f"{fmt_num(c['bounces'])} ({c['bounce_rate']}%)"),
                ("Unique opens", f"{fmt_num(c['unique_opens'])} ({c['open_rate']}%)"),
            ]
            border_style = "border-bottom: 1px dashed var(--line); padding-bottom: 24px; margin-bottom: 32px;" if i < len(campaigns) - 1 else ""
            block = f"""
            <div class="campaign-item" style="{border_style}">
              <div class="campaign-subject" style="font-weight: 600; margin-bottom: 12px;">
                &ldquo;{c['subject']}&rdquo; — sent {fmt_date(c['sent_time'])}
              </div>
              {data_table(campaign_rows)}
            </div>
            """
            campaigns_blocks.append(block)
    campaigns_html = "\n".join(campaigns_blocks)

    subscriber_rows = [
        ("Total subscribers", fmt_num(subs["total_subscribers"])),
        (f"New — this period (since {period_label})", fmt_num(subs["period_new"])),
        (f"Unsubscribed — this period (since {period_label})", fmt_num(subs["period_unsub"])),
        ("Net change — this period", signed(subs["period_net"])),
        (f"New — last 12 months (since {annual_label})", fmt_num(subs["annual_new"])),
        (f"Unsubscribed — last 12 months (since {annual_label})", fmt_num(subs["annual_unsub"])),
        ("Net change — last 12 months", signed(subs["annual_net"])),
    ]

    chart_svg = render_bar_chart(subs["period_new"], subs["period_unsub"], subs["annual_new"], subs["annual_unsub"], period_since, annual_since)

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

  body {{
    background: var(--bg);
    color: var(--ink);
    font-family: 'Inter', sans-serif;
    margin: 0;
    padding: 40px 20px;
    -webkit-font-smoothing: antialiased;
  }}

  .page {{
    background: var(--paper);
    max-width: 680px;
    margin: 0 auto;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05);
    border-radius: 8px;
    overflow: hidden;
  }}

  .masthead {{
    background: var(--ink);
    color: var(--paper);
    padding: 40px 48px 36px;
  }}
  .masthead-label {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    font-weight: 600;
    color: var(--gold);
    margin-bottom: 12px;
  }}
  .masthead h1 {{
    font-family: 'Source Serif 4', serif;
    font-size: 32px;
    font-weight: 700;
    margin: 0 0 4px;
    line-height: 1.15;
  }}
  .masthead .sub {{
    font-size: 14px;
    color: #A2ADB9;
    margin-bottom: 28px;
  }}

  .meta-row {{
    display: flex;
    gap: 40px;
    border-top: 1px solid rgba(255, 255, 255, 0.15);
    padding-top: 24px;
  }}
  .meta-item {{
    flex: 1;
  }}
  .meta-label {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #A2ADB9;
    margin-bottom: 4px;
  }}
  .meta-value {{
    font-size: 14px;
    font-weight: 500;
  }}

  .body {{
    padding: 8px 48px 48px;
  }}

  .section {{
    margin-top: 40px;
  }}
  .section-heading {{
    display: flex;
    align-items: baseline;
    gap: 8px;
    border-bottom: 2px solid var(--ink);
    padding-bottom: 8px;
    margin-bottom: 16px;
  }}
  .section-heading h2 {{
    font-family: 'Source Serif 4', serif;
    font-size: 20px;
    font-weight: 700;
    margin: 0;
  }}
  .section-num {{
    font-family: 'Source Serif 4', serif;
    font-size: 18px;
    color: var(--gold);
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

  .chart-wrap {{ margin-top: 20px; }}
  .hbar-row {{ margin-bottom: 22px; }}
  .hbar-row:last-child {{ margin-bottom: 0; }}
  .hbar-head {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 8px;
  }}
  .hbar-label {{
    font-family: 'Source Serif 4', serif;
    font-size: 14px;
    font-weight: 600;
  }}
  .hbar-range {{
    font-size: 11.5px;
    color: var(--ink-soft);
  }}
  .hbar-track {{
    display: flex;
    height: 14px;
    border-radius: 3px;
    overflow: hidden;
    background: var(--line);
  }}
  .hbar-seg.new {{ background: var(--navy); }}
  .hbar-seg.unsub {{ background: var(--burgundy); }}
  .hbar-legend {{
    display: flex;
    gap: 24px;
    margin-top: 8px;
    font-size: 12px;
    color: var(--ink-soft);
  }}
  .sw {{
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
        {campaigns_html}
      </div>

      <div class="section">
        <div class="section-heading">
          <span class="section-num">2.</span>
          <h2>Subscriber Growth</h2>
        </div>
        {data_table(subscriber_rows)}

        <div class="chart-wrap">
          {chart_svg}
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
    parser = argparse.ArgumentParser(description="Generate a Sender.net performance report page from CSV data.")
    parser.add_argument("--data-dir", type=str, default="data",
                         help="Directory containing the source CSV files.")
    parser.add_argument("--output-dir", type=str, default="docs",
                         help="Directory to write index.html into.")
    parser.add_argument("--sample", action="store_true",
                         help="Generate the page from sample data, without reading CSVs.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path(args.data_dir)

    if args.sample:
        print("Generating page from SAMPLE data...")
        campaign_summary, subs_summary = sample_data()
        campaigns_list = [campaign_summary]
        now = datetime.now(timezone.utc)
        period_since = now - timedelta(days=30)
        annual_since = now - timedelta(days=365)
    else:
        campaigns_file = data_dir / "campaigns.csv"
        subscribers_file = data_dir / "subscribers.csv"

        if not campaigns_file.exists() or not subscribers_file.exists():
            print(f"Error: CSV data files not found in {data_dir}. Run scripts/fetch_data.py first, or run with --sample.", file=sys.stderr)
            sys.exit(1)

        res = load_latest_subscribers(subscribers_file)
        if not res:
            print("Error: Could not load subscriber data from CSV file.", file=sys.stderr)
            sys.exit(1)

        subs_summary, period_since, annual_since = res
        now = datetime.now(timezone.utc)

        campaign_since = now - timedelta(days=31)
        campaigns_list = load_campaigns_in_period(campaigns_file, campaign_since)
        if not campaigns_list:
            # Fallback to the latest campaign in CSV to avoid an empty list
            latest = load_latest_campaign(campaigns_file)
            campaigns_list = [latest] if latest else []

        if not campaigns_list:
            print("Error: Could not load campaign data from CSV file.", file=sys.stderr)
            sys.exit(1)

    html = render_html(campaigns_list, subs_summary, period_since, annual_since, now)

    out_path = output_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")

    print(f"\nPage written to: {out_path}")


if __name__ == "__main__":
    main()
