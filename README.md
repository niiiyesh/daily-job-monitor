# Daily Career Job Monitor

A beginner-friendly Python project that checks selected **public Greenhouse and Lever job boards**, matches jobs by title and location, remembers previously seen job IDs, and sends **Telegram alerts only for newly detected matching vacancies**.

## 1. Run on your Mac

Install Python 3.10+ and open the project in VS Code. In Terminal:

```bash
cd job-monitor-starter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py --dry-run
```

`--dry-run` prints current matches but does NOT save job history or send notifications.

## 2. Choose real companies

Edit `config.json`. For companies using Greenhouse, use the token from a URL such as `https://job-boards.greenhouse.io/COMPANY` (provider `greenhouse`, site `COMPANY`). For Lever, use the slug from `https://jobs.lever.co/COMPANY` (provider `lever`, site `COMPANY`). Some Lever boards use the EU domain, which requires adapting the API base URL in `monitor.py`.

**The active Greenhouse example is for testing only.** It currently lists jobs, but may have no vacancies matching your Italy-based filters. The disabled Lever example is a format illustration, not a guarantee that the site currently lists jobs.

Other companies may use Workday, SuccessFactors, custom sites, and require a different connector. Do not assume any career page can be fetched by this project. Respect site terms, access controls, and rate limits.

Edit `title_keywords`, `exclude_title_keywords`, and `locations` as desired. This first version matches the job **title** and advertised **location**, not the full job description, and does not verify language requirements or visa eligibility.

## 3. Connect Telegram

1. In Telegram, find the verified **@BotFather**, send `/newbot` and follow the instructions.
2. Keep the generated bot token PRIVATE. Open a chat with your new bot and send `/start`.
3. With the virtual environment active, run `python telegram_setup.py`; it will ask for the token privately, print your chat ID, and send a test message.
4. Don't paste your token in a GitHub file, commit, screenshot or public chat.

## 4. Upload to GitHub

Create a GitHub repo called `daily-job-monitor`, preferably **private** while setting up. Upload this folder's CONTENTS to the repository root, including the hidden `.github/workflows/daily-jobs.yml` file. Do NOT upload `.venv` or secrets.

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Add:

- `TELEGRAM_BOT_TOKEN` = the token from BotFather
- `TELEGRAM_CHAT_ID` = the ID shown by `telegram_setup.py`

Then open **Actions → Check career websites → Run workflow**. (If the workflow is not shown, check that `.github/workflows/daily-jobs.yml` was uploaded under the correct nested path.)

The workflow runs every day at **08:17 Europe/Rome**, though GitHub's schedule can be delayed or skipped at times. It will commit updated history to `data/seen_jobs.json`, so it must have `contents: write` permission and permission to push to the default branch. Branch protection may require a different persistence strategy.

## 5. Important first-run behavior

The first **successful check for each company** collects current job IDs as the baseline and sends no job alerts. After that, newly appearing matching jobs can trigger notifications. This avoids claiming months-old postings are new.

If a job-alert message cannot be sent, that job ID is deliberately left unmarked and can be retried on a later run.

## Project files

- `monitor.py`: extract public jobs, filter and send notifications
- `config.json`: company slugs and filtering preferences
- `telegram_setup.py`: test a Telegram connection
- `data/seen_jobs.json`: persistent history, updated automatically
- `tests/test_monitor.py`: three offline unit tests
- `.github/workflows/daily-jobs.yml`: scheduled GitHub Actions workflow
- `requirements.txt`, `.gitignore`, `README.md`: installation, security and documentation

## Next possible upgrades

- More ATS connectors (Workday, SmartRecruiters, custom sites)
- Better deduplication when the same vacancy has multiple job-board IDs
- NLP / AI-based scoring from the description against your CV
- Daily summary, applications tracker, Streamlit dashboard
- SQLite database with persistent remote storage (the starter uses JSON to keep GitHub persistence simple)
