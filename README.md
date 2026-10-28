# Gmail Job Application Tracker

Applied to 60 jobs and lost track? This script reads your Gmail (read-only), finds every
application confirmation, assessment invite, interview request, rejection and offer, and turns
them into one table with the current status of each application.

```
$ python gmail_jobs.py --since 2026/09/01
151 candidate emails since 2026/09/01 (151 new to fetch)
89 job emails -> 60 companies: Applied 49, Assessment 2, Interview 2, Rejected 7
Open output/report.html
```

| Company | Role | Status | Applied | Last update |
|---|---|---|---|---|
| Globex | Data Science Intern | Interview | 2026-10-15 | 2026-10-18 |
| Initech | ML Engineer Co-op | Assessment | 2026-09-28 | 2026-09-29 |
| Acme Robotics | Data Analyst Intern | Rejected | 2026-09-21 | 2026-10-23 |
| Hooli | ML Intern (Winter 2027) | Applied | 2026-10-26 | 2026-10-26 |

*(example rows, not real data)*

## Features

- **Finds application emails automatically**: Workday, Greenhouse, Lever, iCIMS, Ashby,
  SuccessFactors, Taleo, SmartRecruiters and 30+ other hiring systems, plus LinkedIn Easy Apply
  and Indeed. Test platforms (HackerRank, Codility, HireVue...) count as assessments.
- **Tracks status**: Applied → Assessment → Interview → Offer, or Rejected. The newest real
  update wins, so a rejection after an assessment shows as Rejected.
- **Cleans up company names**: "Globex Inc. @ icims", "GlobexWorkday" and a recruiter's
  personal email all become the right company.
- **Skips the noise**: job alerts, "jobs you may like", newsletters and password resets.
- **Fast re-runs**: emails are cached locally, so later runs only download new mail.
- **Private**: read-only Gmail access, everything stays on your computer.

## Privacy

- The script asks Google for **read-only** access (`gmail.readonly`). It cannot send, delete or
  change anything.
- Nothing leaves your computer. There is no server, no analytics, no third party.
- Three local files are sensitive. They are already in `.gitignore`, so **never commit or share
  them**:
  - `credentials.json` is your Google OAuth client.
  - `token.json` is your saved sign-in.
  - `cache/` holds the text of your job emails.
- To revoke access at any time, go to https://myaccount.google.com/permissions.

## Setup (about 10 minutes)

### 1. Get the code and install

Requires Python 3.9 or newer.

```bash
git clone https://github.com/bhuvanchandar7/gmail-job-tracker.git
cd gmail-job-tracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` instead of `.venv/bin/...`.

### 2. Create a Google Cloud app (one time)

Google needs to know which app is reading your mail. You create your own app, so you don't
have to trust anyone else's.

1. Go to https://console.cloud.google.com/ and **create a project** (any name, e.g. "job tracker").
2. Open **APIs & Services → Library**, search for **Gmail API**, and click **Enable**.
3. Open **Google Auth Platform** (older consoles call it "OAuth consent screen") and click
   **Get started**:
   - App name: anything. Support email: your Gmail.
   - Audience: **External**.
   - Contact email: your Gmail. Accept the terms and click **Create**.
4. Go to **Audience → Test users → Add users** and add **your own Gmail address**.
   If you skip this, sign-in fails with "Access blocked".
5. Go to **Clients → Create client**, choose **Desktop app**, and click **Create**.
6. Click **Download JSON**, rename the file to `credentials.json`, and put it in the
   `gmail-job-tracker` folder.

### 3. First run

```bash
.venv/bin/python gmail_jobs.py --since 2026/09/01
```

A browser tab opens for you to sign in to Google:

1. Pick your account.
2. Google warns **"Google hasn't verified this app"**. That's expected, since it's your own
   app. Click **Continue**.
3. Allow **"View your email messages and settings"**.

The script then downloads and sorts your emails. Open `output/report.html` in any browser.

## Usage

```bash
.venv/bin/python gmail_jobs.py                     # last 180 days
.venv/bin/python gmail_jobs.py --since 2026/09/01   # emails after a date (YYYY/MM/DD)
.venv/bin/python gmail_jobs.py --refresh            # ignore the cache and re-download everything
.venv/bin/python gmail_jobs.py --headless           # never open a browser (for scheduled runs)
```

| Output file | What's in it |
|---|---|
| `output/report.html` | Table you can search, filter by status and sort, with a link to each Gmail thread |
| `output/applications.csv` | One row per company: status, roles, applied date, last update. Opens in Excel or Google Sheets |
| `output/emails.csv` | Every matched email with its detected stage. Use it to spot parser mistakes |

## Customize

**Merge company names.** If one employer shows up under two names (e.g. "JPMC" and
"JPMorgan Chase"), copy `aliases.example.json` to `aliases.json` and add a line:

```json
{
  "jpmc": "JPMorgan Chase",
  "meta platforms": "Meta"
}
```

Keys are the company name in lowercase with punctuation and words like Inc, Ltd and Careers
removed. `aliases.json` is ignored by git, so your list stays private.

**Other tweaks** are near the top of `gmail_jobs.py`:

| Setting | Use it to |
|---|---|
| `ATS_DOMAINS` | Add a hiring system the script doesn't know |
| `NOISE_SUBJECT`, `NOISE_SENDERS` | Hide emails that aren't applications |
| `STAGES`, `SUBJECT_STAGES` | Add phrases that mean applied / rejected / interview / offer |

After changing the parser, re-run the script. It re-parses the cached emails without downloading
them again.

## Run it automatically every week

Use `--headless` so a scheduled run never gets stuck waiting for a browser sign-in. If the
sign-in has expired, the script exits with code `3` and prints `GMAIL_AUTH_EXPIRED`.

**macOS / Linux (cron).** Run `crontab -e` and add this line (every Monday at 9 AM):

```
0 9 * * 1 cd /path/to/gmail-job-tracker && .venv/bin/python gmail_jobs.py --headless >> output/cron.log 2>&1
```

On macOS, cron can't read folders like Desktop or Documents unless you give `cron` Full Disk
Access (System Settings → Privacy & Security). Keeping the project somewhere else avoids that.

**Windows.** Create a weekly task in Task Scheduler that runs
`.venv\Scripts\python.exe gmail_jobs.py --headless` with the project folder as "Start in".

**Stop the 7-day sign-in expiry.** While your Google app is in **Testing** mode, Google expires
the sign-in after 7 days. For weekly runs, go to **Google Auth Platform → Audience** and click
**Publish app** to move it to **In production**. For an app only you use, Google normally
allows this without verification; you'll just keep seeing the "unverified app" warning when you
sign in. Then
delete `token.json` and run the script once to sign in again.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Missing credentials.json` | Finish setup step 2 and put the file in the project folder |
| "Access blocked: app has not completed verification" | Add your Gmail under **Audience → Test users** |
| "Gmail rate limit hit, waiting 15s..." | Normal for new Google projects. The script waits and continues on its own |
| `GMAIL_AUTH_EXPIRED` or `invalid_grant` | Delete `token.json` and run the script again to sign in |
| A company has the wrong name, or shows up twice | Add it to `aliases.json` |
| An application is missing | The confirmation probably used unusual wording. Find it in Gmail, then add a phrase to `STAGES` or a sender to `ATS_DOMAINS` |
| A confirmation is marked as Rejected or Interview | Check its row in `emails.csv`, then adjust the phrase in `STAGES` |

## How it works

1. **Search.** It asks Gmail for emails whose subject mentions applying, an application, an
   interview, an assessment or an offer, or that come from a known hiring system.
2. **Classify.** The subject line is trusted first, because it is short and deliberate
   ("Thanks for applying", "Interview request"). Body text only counts if it is near the top
   and not hypothetical: "if you are selected for an interview" doesn't count as an interview.
3. **Name the company.** It tries the sender's name, then the sender's domain (a Workday
   tenant like `ibm@myworkday.com` means IBM), then phrases like "your application to X".
4. **Summarize.** It groups emails by company, and the latest real update decides the status.

## Run the tests

The tests use made-up emails, so they need no Gmail account and no installed packages:

```bash
python3 tests/test_parser.py
```

## Limitations

- Matching is rule-based, so unusual email wording can be missed or mislabeled. Check
  `emails.csv` the first time you run it.
- It only sees what reaches your inbox. An application with no confirmation email won't appear.
- Some emails don't name the role, so that column can be blank.

## License

[MIT](LICENSE)
