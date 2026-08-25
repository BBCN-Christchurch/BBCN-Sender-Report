# Sender.net Performance Report — GitHub-hosted

A self-updating report page: a scheduled GitHub Actions workflow pulls your
latest campaign performance and subscriber growth from the Sender.net API
and publishes it as a live GitHub Pages site. No server to run, no token
ever exposed in the page itself.

## How it works

- `scripts/generate_report.py` calls the Sender.net API and writes
  `docs/index.html` — a single self-contained HTML file.
- `.github/workflows/update-report.yml` runs that script automatically on
  the 1st of every month, and commits the refreshed page back to the repo.
- GitHub Pages serves whatever is in `docs/` on your default branch — so
  every automated commit updates the live page.
- Your API token is stored as an encrypted **GitHub Actions secret**. It's
  never written into `index.html`, never visible in the page source, and
  never committed to the repo.

## 1. Create the repository

Create a new GitHub repository (private or public — see note below) and
push these files to it:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

> **Public vs private:** GitHub Pages works on private repos too if you're
> on a paid GitHub plan; on the free plan, Pages sites from private repos
> are only viewable by people with repo access, while Pages from a public
> repo is visible to anyone with the URL. Either way, the *repo's code* is
> visible to whoever can see the repo — but your API token is not in the
> code, so this is safe regardless.

## 2. Add your API token as a secret

In your repository: **Settings → Secrets and variables → Actions → New
repository secret**

- Name: `SENDER_API_TOKEN`
- Value: your Sender.net API token (Settings → API access tokens in Sender.net)

> Since your token was previously pasted into a chat conversation, it's
> worth rotating it in Sender.net first (revoke the old one, generate a
> new one) and using the fresh token here.

## 3. Enable GitHub Pages

**Settings → Pages**
- Source: **Deploy from a branch**
- Branch: `main`, folder: `/docs`
- Save

Your report will be live at `https://YOUR-USERNAME.github.io/YOUR-REPO/`
once the first workflow run completes.

## 4. Run it for the first time

Go to the **Actions** tab → "Update Sender.net Report" workflow → **Run
workflow**. This is also how you manually refresh the page any time you
want, in addition to the automatic monthly run.

After that, it runs itself: 09:00 UTC on the 1st of each month, via the
`cron` schedule in the workflow file. Edit that line in
`.github/workflows/update-report.yml` if you'd like a different day/time.

## Previewing locally before you push

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python scripts/generate_report.py --sample
open docs/index.html            # or just double-click it
```

`--sample` uses placeholder data and makes no API calls, so you can check
the design without a token. To test against your real account locally:

```bash
cp .env.example .env
# edit .env and paste your token in
python scripts/generate_report.py
```

## What's shown

**Latest campaign** (most recently *sent* campaign): sends, delivered
(sent minus bounces), unique opens (de-duplicated by recipient, with open
rate), bounces (with bounce rate).

**Subscribers:** current total, plus two comparison panels:
- **This period** — since the last time the report ran (or the last 30
  days on the very first run)
- **Last 12 months** — a rolling annual view

Both panels show new subscribers, unsubscribes, net change, and a
proportional bar showing the new-vs-unsubscribed composition. No
individual subscriber emails are listed or stored anywhere — only totals.

## Notes on the automation

- The "since last run" window for the period panel is tracked in
  `.state/last_run.json`, which the workflow commits back to the repo
  after each run — this is why the workflow needs `permissions: contents:
  write`.
- If a scheduled run is skipped (GitHub doesn't guarantee cron runs to the
  minute on inactive repos), the next run's "period" window simply covers
  the extra time — nothing is lost.
- Large subscriber lists take a little longer to process since the script
  pages through the full list to compute accurate new/unsubscribed counts.
  The script backs off automatically on API rate limits.
