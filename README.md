# Paper Tracker

Journal tracker for recent papers in management, strategy, innovation,
entrepreneurship, international business, and information systems journals.
It can run locally or as a small public web service.

## Quick start

```bash
python3 paper_tracker.py fetch --days 365
python3 paper_tracker.py serve --port 8765
```

Then open:

```text
http://127.0.0.1:8765
```

To protect the site with a browser login prompt:

```bash
export PAPER_TRACKER_USER="paper"
export PAPER_TRACKER_PASSWORD="your_password"
python3 paper_tracker.py serve --port 8765
```

The public page does not include a manual update button. Updates should be
triggered by the protected Cron endpoint.

## What it stores

- journal
- title
- authors
- abstract, when OpenAlex provides one
- publication date
- DOI
- article link

The article link is usually the publisher landing page or a DOI URL. The app
does not fetch or store PDFs.

Harvard Business Review is tracked through its public Atom feed because it is
not updated like a conventional academic journal in OpenAlex. Sponsored and
podcast links are skipped by default.

## Optional API settings

OpenAlex works without local setup in many cases, but using an API key or email
is more reliable for repeated tracking:

```bash
export OPENALEX_API_KEY="your_key"
# or
export OPENALEX_MAILTO="you@example.com"
```

## Environment variables

- `PAPER_TRACKER_DB`: SQLite database path. Use `/data/papers.db` on Railway
  with a mounted volume.
- `PAPER_TRACKER_USER`: login username. Defaults to `paper`.
- `PAPER_TRACKER_PASSWORD`: login password. If unset, the site is open.
- `PAPER_TRACKER_CRON_SECRET`: secret token for the protected daily update
  endpoint.
- `PAPER_TRACKER_PUBLIC_URL`: deployed site URL, used by the Cron trigger
  command.
- `PAPER_TRACKER_CRON_DAYS`, `PAPER_TRACKER_CRON_PER_PAGE`,
  `PAPER_TRACKER_CRON_PAGES`: optional daily update range. Defaults to
  14 days, 50 records per page, 2 pages.
- `PORT`: platform-provided port. Railway sets this automatically.
- `OPENALEX_API_KEY` or `OPENALEX_MAILTO`: optional OpenAlex polite-pool
  settings.

## Railway deployment

1. Create a private GitHub repository and push this project.
2. In Railway, create a new project from the GitHub repository.
3. Add a service variable:

```text
PAPER_TRACKER_DB=/data/papers.db
PAPER_TRACKER_USER=paper
PAPER_TRACKER_PASSWORD=<choose-a-strong-password>
PAPER_TRACKER_CRON_SECRET=<choose-a-second-strong-secret>
OPENALEX_MAILTO=<your-email>
```

4. Add a Railway volume mounted at:

```text
/data
```

5. Deploy the web service. `Procfile` starts the app with:

```bash
python3 paper_tracker.py serve --host 0.0.0.0
```

The app reads Railway's `PORT` environment variable automatically.

6. Set the health check path to:

```text
/healthz
```

7. After the first deployment, copy the public Railway URL or your custom
   domain and add it as:

```text
PAPER_TRACKER_PUBLIC_URL=https://papers.example.com
```

8. Add a Railway Cron service for daily updates at 6:00 every morning with
   this command:

```bash
python3 paper_tracker.py trigger-remote-fetch
```

The Cron service only needs these variables:

```text
PAPER_TRACKER_PUBLIC_URL=https://papers.example.com
PAPER_TRACKER_CRON_SECRET=<same-secret-as-the-web-service>
```

It does not need the SQLite volume. The Cron service securely calls the web
service, and the web service updates its own `/data/papers.db`.

9. Add a custom domain in Railway, for example:

```text
papers.example.com
```

Then add the DNS record Railway shows in your domain provider.

## Daily update with cron

```cron
0 9 * * * cd /Users/zhaochengeng/Documents/论文追索 && /usr/bin/python3 paper_tracker.py fetch --days 14
```

## Notes

The journal list lives in `journals.csv`. Search and filtering run against the
local SQLite database `papers.db`.
