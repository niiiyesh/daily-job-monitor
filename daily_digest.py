"""Daily Telegram digest of CURRENT matching vacancies, independent of new-job alerts.

Run locally: python daily_digest.py --dry-run
Live: TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python daily_digest.py

This module reuses the existing monitor's configuration, ATS connectors, and
filters. It intentionally NEVER reads or updates data/seen_jobs.json.
"""

import argparse
import csv
from datetime import datetime
from io import StringIO
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from monitor import CONFIG_PATH, TIMEOUT, fetch_jobs, matches

ROME = ZoneInfo("Europe/Rome")
MAX_TELEGRAM_MESSAGE = 4096


def collect_daily_jobs(config):
    """Check all enabled companies. A failing source is flagged, not counted as zero."""
    groups = []
    for company in config.get("companies", []):
        if not company.get("enabled", True):
            continue
        name = company["name"]
        try:
            fetched = fetch_jobs(company)
            company_config = {
                **config,
                "locations": company.get("locations", config.get("locations", [])),
            }
            unique = {}
            for job in fetched:
                if matches(job, company_config):
                    identifier = str(job.get("id") or job.get("url") or "")
                    if identifier and identifier not in unique:
                        unique[identifier] = job
            jobs = sorted(unique.values(), key=lambda job: job.get("title", "").casefold())
            source_date = next(
                (item.get("source_last_updated", "") for item in fetched if item.get("source_last_updated")),
                "",
            )
            groups.append({
                "name": name,
                "provider": company["provider"].lower(),
                "total": len(fetched),
                "jobs": jobs,
                "source_last_updated": source_date,
                "error": False,
            })
            print(f"{name}: {len(fetched)} retrieved; {len(jobs)} matching")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            # Do not leak URLs with credentials into logs. These sources should
            # be public, but only expose the company name and error category.
            print(f"WARNING: {name} failed ({type(exc).__name__})")
            groups.append({"name": name, "provider": company["provider"].lower(),
                           "total": None, "jobs": [], "source_last_updated": "", "error": True})
    if not groups:
        raise RuntimeError("No companies are enabled in config.json")
    if all(group["error"] for group in groups):
        raise RuntimeError("All career sources failed; no digest was sent")
    return groups


def _cell(value):
    """Prevent formula execution when the CSV is opened in Excel/Sheets."""
    value = " ".join(str(value or "").split())
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def make_csv(groups):
    """All matched vacancies, grouped by source, as Excel-compatible UTF-8 CSV."""
    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["Company", "Job title", "Location", "Application link", "Source", "Source last updated", "Caution"])
    for group in groups:
        if group["error"]:
            continue
        supplementary = group["provider"] == "stellantis_torino"
        caution = ("Supplementary curated list; posting may be closed. Verify directly."
                   if supplementary else "Verify vacancy remains open before applying.")
        for job in group["jobs"]:
            writer.writerow([
                _cell(group["name"]), _cell(job.get("title")), _cell(job.get("location")),
                _cell(job.get("url")), _cell(group["provider"]),
                _cell(group["source_last_updated"]), caution,
            ])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _clip(value, max_length=90):
    clean = " ".join(str(value or "").split())
    return clean if len(clean) <= max_length else clean[:max_length - 1] + "…"


def make_message(groups, day):
    total_matching = sum(len(group["jobs"]) for group in groups)
    ok_sources = sum(not group["error"] for group in groups)
    lines = [
        f"📋 DAILY JOB SUMMARY — {day:%d %b %Y}",
        f"{total_matching} matching listings • {ok_sources}/{len(groups)} sources checked",
        "Italy • Management Engineering roles • non-senior",
        "Language requirements are NOT filtered.",
        "",
    ]
    for group in groups:
        if group["error"]:
            lines.append(f"⚠️ {group['name']}: source unavailable today")
            continue
        count = len(group["jobs"])
        lines.append(f"🏢 {_clip(group['name'], 65)}: {count} matches ({group['total']} listed)")
        for job in group["jobs"][:2]:
            lines.append(f"   • {_clip(job.get('title'), 76)}")
        if count > 2:
            lines.append(f"   + {count - 2} more in attached CSV")
        if group["provider"] == "stellantis_torino":
            updated = group["source_last_updated"] or "unknown"
            lines.append(f"   ⚠️ Turin curated list (updated {updated}); confirm jobs are still open")
    lines.extend([
        "",
        "📎 The attached CSV includes EVERY matching listing and its application link.",
        "These are advertised listings, not verified application availability.",
        "New-job alerts continue separately.",
    ])
    msg = "\n".join(lines)
    # Telegram sendMessage max is 4096 characters; truncate defensive only.
    if len(msg) > MAX_TELEGRAM_MESSAGE:
        msg = msg[:MAX_TELEGRAM_MESSAGE - 110] + "\n\n📎 See the attached CSV for the full listing."
    return msg


def _telegram_post(token, endpoint, **kwargs):
    """Do not print bot token, raw API URL, or Telegram response on errors."""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{endpoint}",
            timeout=TIMEOUT,
            **kwargs,
        )
    except requests.RequestException:
        raise RuntimeError(f"Telegram {endpoint} network request failed") from None
    if response.status_code != 200:
        raise RuntimeError(f"Telegram {endpoint} HTTP {response.status_code}")
    try:
        ok = response.json().get("ok")
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        raise RuntimeError(f"Telegram {endpoint} rejected the request")


def send_daily_digest(groups, token, chat_id, day):
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secrets")
    text = make_message(groups, day)
    file_bytes = make_csv(groups)
    filename = f"matching_jobs_{day:%Y-%m-%d}.csv"
    # One readable summary, plus ONE downloadable attachment (not 137 alerts).
    _telegram_post(token, "sendMessage", json={"chat_id": chat_id, "text": text})
    _telegram_post(token, "sendDocument", data={"chat_id": chat_id},
                   files={"document": (filename, file_bytes, "text/csv")})
    print(f"Daily Telegram summary and {filename} sent")


def main(dry_run=False):
    config = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    groups = collect_daily_jobs(config)
    day = datetime.now(ROME)
    if dry_run:
        print("\n" + make_message(groups, day))
        print(f"\nCSV would contain {sum(len(g['jobs']) for g in groups)} rows; no messages sent.")
        return
    send_daily_digest(groups, os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                      os.environ.get("TELEGRAM_CHAT_ID", ""), day)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a daily Telegram digest of matching jobs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview counts without sending Telegram or altering history")
    main(dry_run=parser.parse_args().dry_run)
