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
- optional Chinese title and abstract translations, generated on demand

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

Chinese title and abstract translation is optional. The web page only requests
a translation after someone clicks a paper's translation button. Translations
are cached in SQLite so the same paper does not need to be translated again.
By default the app uses DeepL when `DEEPL_API_KEY` is configured; otherwise it
falls back to MyMemory, which does not require a payment method but has a much
smaller free daily quota and lower translation quality:

```bash
export PAPER_TRACKER_TRANSLATION_PROVIDER="auto"
export MYMEMORY_SOURCE_LANG="en"
export MYMEMORY_TARGET_LANG="zh-CN"
export MYMEMORY_EMAIL="you@example.com"
# Optional, for higher-quality DeepL translations:
# export DEEPL_API_KEY="your_deepl_api_key"
# export DEEPL_TARGET_LANG="ZH"
# export DEEPL_SOURCE_LANG="EN"
```

Email notification is optional. When SMTP variables are configured, each fetch
run sends one digest email if new papers were inserted. No email is sent when
there are no new papers:

```bash
export RESEND_API_KEY="re_your_resend_api_key"
export RESEND_FROM="Paper Tracker <onboarding@resend.dev>"
# or use SMTP where outbound SMTP is supported:
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="465"
export SMTP_USER="you@example.com"
export SMTP_PASSWORD="your_smtp_authorization_code"
export MAIL_FROM="you@example.com"
export NOTIFY_EMAIL_TO="you@example.com"
```

Railway Hobby/Free deployments may not be able to reach SMTP servers directly,
so an HTTPS email API such as Resend is recommended there. For many mailboxes,
`SMTP_PASSWORD` is an app password or SMTP authorization code, not the account
login password. Port `465` uses SSL by default. Port `587` uses STARTTLS by
default.

Users can also subscribe from the web page's email reminder panel. Those
addresses are stored in SQLite and are included with `NOTIFY_EMAIL_TO` when
new-paper digest emails are sent. The web page only shows subscription counts,
not the actual email addresses. If the public site is not protected with
`PAPER_TRACKER_PASSWORD`, anyone who can open the site can add subscriber
emails.
The same panel can clear all stored subscriber emails without revealing them.

When SMTP is configured, the app sends a short confirmation email immediately
after a user subscribes from the web page. This confirmation is sent in the
background so slow SMTP connections do not block the subscription request. If
SMTP is not configured, the address is still saved, but no confirmation email
is sent.
The email reminder panel also includes a test-email button that sends one
message to the address in the input field and reports SMTP errors directly.

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
- `PAPER_TRACKER_TRANSLATION_PROVIDER`: optional translation provider:
  `auto`, `deepl`, or `mymemory`. Defaults to `auto`.
- `MYMEMORY_SOURCE_LANG`: optional MyMemory source language. Defaults to `en`.
- `MYMEMORY_TARGET_LANG`: optional MyMemory target language. Defaults to
  `zh-CN`.
- `MYMEMORY_EMAIL`: optional MyMemory contact email. Recommended for more
  generous free usage limits.
- `DEEPL_API_KEY`: optional DeepL API key. When configured, `auto` mode uses
  DeepL for the on-demand Chinese translation button.
- `DEEPL_TARGET_LANG`: optional DeepL target language. Defaults to `ZH`.
- `DEEPL_SOURCE_LANG`: optional DeepL source language. Defaults to `EN`.
- `DEEPL_API_URL`: optional DeepL API base URL. Defaults to the Free API URL
  for Free keys and the Pro URL for Pro keys.
- `RESEND_API_KEY`, `RESEND_FROM`: optional Resend HTTPS email API settings.
  When configured, Resend is used before SMTP.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`: optional SMTP
  fallback settings for digest emails when new papers are inserted.
- `SMTP_SSL`: optional boolean. Defaults to true when `SMTP_PORT=465`.
- `SMTP_STARTTLS`: optional boolean. Defaults to true when SSL is disabled.
- `SMTP_TIMEOUT`: optional SMTP connection timeout in seconds. Defaults to `8`.
- `MAIL_FROM`: optional sender address. Defaults to `SMTP_USER`.
- `NOTIFY_EMAIL_TO`: optional comma-separated recipient list for digest
  emails. Web-page subscribers are also included.
- `NOTIFY_EMAIL_SUBJECT_PREFIX`: optional subject prefix. Defaults to
  `论文追索`.
- `NOTIFY_MAX_PAPERS`: optional maximum number of papers listed in one email.
  Defaults to `50`.

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
PAPER_TRACKER_TRANSLATION_PROVIDER=auto
MYMEMORY_EMAIL=<your-email>
RESEND_API_KEY=<your-resend-api-key>
RESEND_FROM=Paper Tracker <onboarding@resend.dev>
SMTP_HOST=<your-smtp-host>
SMTP_PORT=465
SMTP_USER=<your-email>
SMTP_PASSWORD=<your-smtp-authorization-code>
MAIL_FROM=<your-email>
NOTIFY_EMAIL_TO=<recipient-email>
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
