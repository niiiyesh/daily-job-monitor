"""Check public ATS job boards and alert on newly published matching roles."""

import argparse
import json
import os
from pathlib import Path

import requests
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "seen_jobs.json"
TIMEOUT = 25


def fetch_jobs(company):
    """Return normalized public job postings for a supported ATS."""
    provider = company["provider"].lower()
    site = company["site"].strip().strip("/")
    results = []

    if provider == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{site}/jobs"
        response = requests.get(url, params={"content": "true"}, timeout=TIMEOUT)
        response.raise_for_status()
        for job in response.json()["jobs"]:
            results.append({
                "id": str(job["id"]),
                "title": job.get("title", ""),
                "location": (job.get("location") or {}).get("name", ""),
                "url": job.get("absolute_url", ""),
                "description": job.get("content", ""),
            })

    elif provider == "lever":
        # Lever supports paginated public postings. Collect all pages.
        url = f"https://api.lever.co/v0/postings/{site}"
        limit = 100
        for offset in range(0, 10000, limit):
            response = requests.get(url, params={"mode": "json", "skip": offset, "limit": limit}, timeout=TIMEOUT)
            response.raise_for_status()
            page = response.json()
            for job in page:
                categories = job.get("categories") or {}
                locations = categories.get("allLocations") or []
                location = ", ".join(locations) if locations else categories.get("location", "")
                results.append({
                    "id": str(job["id"]),
                    "title": job.get("text", ""),
                    "location": location or "",
                    "url": job.get("hostedUrl") or job.get("applyUrl") or "",
                    "description": job.get("descriptionPlain", ""),
                })
            if len(page) < limit:
                break
    elif provider == "ashby":
        # Official public Ashby job-postings API (no credentials needed).
        url = f"https://api.ashbyhq.com/posting-api/job-board/{site}"
        response = requests.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        for job in response.json().get("jobs", []):
            if job.get("isListed") is False:
                continue  # Do not surface unlisted jobs.
            secondary = [entry.get("location", "") for entry in (job.get("secondaryLocations") or [])]
            locations = [job.get("location", "")] + secondary
            address = (job.get("address") or {}).get("postalAddress") or {}
            country = address.get("addressCountry", "")
            location = ", ".join(dict.fromkeys(str(x) for x in locations + [country] if x))
            job_url = job.get("jobUrl") or job.get("applyUrl") or ""
            if not job_url:
                continue
            results.append({
                "id": str(job.get("id") or job_url),
                "title": job.get("title", ""),
                "location": location,
                "url": job_url,
                "description": job.get("descriptionPlain", ""),
            })

    elif provider == "workable":
        # Workable's documented public jobs endpoint (not the private SPI API).
        url = f"https://www.workable.com/api/accounts/{site}"
        response = requests.get(url, params={"details": "true"}, timeout=TIMEOUT)
        response.raise_for_status()
        for job in response.json().get("jobs", []):
            job_url = job.get("shortlink") or job.get("application_url") or job.get("url") or ""
            job_id = job.get("shortcode") or job.get("code") or job_url
            if not job_id or not job_url:
                continue
            location = ", ".join(str(job[field]) for field in ("city", "state", "country") if job.get(field))
            results.append({
                "id": str(job_id),
                "title": job.get("title", ""),
                "location": location,
                "url": job_url,
                "description": job.get("description", ""),
            })

    elif provider == "amazon":
        # Amazon/AWS share the same public careers board. The JSON search
        # endpoint is publicly readable but undocumented and may change.
        endpoint = "https://www.amazon.jobs/en/search.json"
        limit = 100
        for offset in range(0, 10000, limit):
            response = requests.get(
                endpoint,
                params={
                    "offset": offset,
                    "result_limit": limit,
                    "sort": "recent",
                    "base_query": "",
                    "normalized_country_code[]": "ITA",
                },
                timeout=TIMEOUT,
                headers={"User-Agent": "CareerMonitor/1.0 (personal job alerts)"},
            )
            response.raise_for_status()
            payload = response.json()
            page = payload.get("jobs")
            if not isinstance(page, list):
                raise ValueError("Amazon returned an unexpected jobs response")
            for job in page:
                # Validate country locally as well; never trust an API filter alone.
                if (job.get("country_code") or "").upper() != "ITA":
                    continue
                path = job.get("job_path") or ""
                identifier = job.get("id_icims")
                if identifier is None or not path:
                    continue
                location = job.get("location") or job.get("normalized_location") or job.get("city") or ""
                results.append({
                    "id": str(identifier),
                    "title": job.get("title", ""),
                    "location": f"{location}, Italy" if location else "Italy",
                    "url": urljoin("https://www.amazon.jobs", path),
                    "description": job.get("description", ""),
                })
            if len(page) < limit:
                break

    else:
        raise ValueError(f"Unsupported provider '{provider}' for {company['name']}")

    return results


def matches(job, config):
    """Simple, explainable filter (not yet AI ranking)."""
    title = job["title"].casefold()
    location = job["location"].casefold()
    positive = config.get("title_keywords", [])
    excluded = config.get("exclude_title_keywords", [])
    places = config.get("locations", [])

    role_ok = not positive or any(word.casefold() in title for word in positive)
    seniority_ok = not any(word.casefold() in title for word in excluded)
    location_ok = not places or any(place.casefold() in location for place in places)
    return role_ok and seniority_ok and location_ok


def send_telegram(job, company_name, token, chat_id):
    """Send one alert and raise if delivery fails (so we can retry tomorrow)."""
    msg = (f"NEW JOB | {company_name}\n"
           f"{job['title']}\n"
           f"Location: {job['location'] or 'Not specified'}\n"
           f"Apply: {job['url']}")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": msg[:4000]},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram reported unsuccessful delivery")


def main(dry_run=False):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        state = {}

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    enabled = [c for c in config["companies"] if c.get("enabled", True)]
    if not enabled:
        print("No active companies. Enable one in config.json.")
        return

    for company in enabled:
        key = f"{company['provider'].lower()}:{company['site'].lower()}"
        try:
            jobs = fetch_jobs(company)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            print(f"ERROR: Cannot read {company['name']}: {error}")
            continue  # Do not change state for an unsuccessful source.

        # Allow each company to use its own geographical filter.
        company_config = {**config, "locations": company.get("locations", config.get("locations", []))}
        relevant = [job for job in jobs if matches(job, company_config)]
        print(f"{company['name']}: {len(jobs)} total, {len(relevant)} matching roles.")
        if dry_run:
            for job in relevant[:10]:
                print(f"  {job['title']} | {job['location']} | {job['url']}")
            continue

        if key not in state:
            # First successful run: establish a baseline, rather than alerting
            # about vacancies that might have been published months ago.
            state[key] = sorted({job["id"] for job in jobs})
            print("  First run: saved current postings as baseline; no alerts sent.")
            continue

        seen = set(state[key])
        for job in jobs:
            job_id = job["id"]
            if job_id in seen:
                continue
            if not matches(job, company_config):
                seen.add(job_id)
                continue
            if not (token and chat_id):
                print(f"  NEW MATCH (no Telegram secrets yet): {job['title']} - {job['url']}")
                continue  # Retry on next run, rather than claiming we sent it.
            try:
                send_telegram(job, company["name"], token, chat_id)
                print(f"  Notified: {job['title']}")
                seen.add(job_id)
            except (requests.RequestException, RuntimeError) as error:
                print(f"  ALERT FAILED (will retry): {job['title']}: {error}")
        state[key] = sorted(seen)

    if not dry_run:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("Saved job history.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor public job boards")
    parser.add_argument("--dry-run", action="store_true", help="Preview matching roles; do not send alerts or change history")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
