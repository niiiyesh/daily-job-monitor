"""Check public ATS job boards and alert on newly published matching roles."""

import argparse
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "seen_jobs.json"
TIMEOUT = 25




class ContentsCareersParser(HTMLParser):
    """Read visible job accordions from the official Contents.com careers page.

    This is purposefully scoped to #job_accordion > div.level so navigation,
    footer and unrelated blog content cannot accidentally become vacancies.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.accordion_depth = None
        self.level_depth = None
        self.expanded_depth = None
        self.current = None
        self.jobs = []
        self.saw_accordion = False
        self.ignored_text = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.ignored_text += 1
        if tag == "div":
            self.div_depth += 1
            classes = (attrs.get("class") or "").split()
            if attrs.get("id") == "job_accordion":
                self.saw_accordion = True
                self.accordion_depth = self.div_depth
            elif (self.accordion_depth is not None and self.current is None
                  and "level" in classes and (attrs.get("id") or "").startswith("job-")):
                self.level_depth = self.div_depth
                self.current = {"id": attrs["id"], "title": "", "url": "", "description_parts": []}
            elif self.current is not None and "expanded" in classes:
                self.expanded_depth = self.div_depth
        elif tag == "a" and self.current is not None:
            classes = (attrs.get("class") or "").split()
            if "opening" in classes:
                self.current["title"] = (attrs.get("title") or "").strip()
            elif "perma-to-job-opening" in classes:
                self.current["url"] = (attrs.get("href") or "").strip()

    def handle_data(self, data):
        if self.current is not None and self.expanded_depth is not None and not self.ignored_text:
            self.current["description_parts"].append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored_text = max(0, self.ignored_text - 1)
        if tag != "div":
            return
        if self.current is not None and self.level_depth == self.div_depth:
            self.jobs.append(self.current)
            self.current = None
            self.level_depth = None
            self.expanded_depth = None
        elif self.expanded_depth == self.div_depth:
            self.expanded_depth = None
        if self.accordion_depth == self.div_depth:
            self.accordion_depth = None
        self.div_depth = max(0, self.div_depth - 1)


def contents_job_location(title, description):
    """Infer only explicit geographic mentions; never filter by language."""
    title = title.casefold()
    intro = description[:1800].casefold()
    if any(term in title for term in ("doha", "qatar", "gcc market")):
        return "Doha, Qatar"
    if any(term in title for term in ("milan", "milano")) or re.search(r"\b(?:base|based) in milan\b", intro):
        return "Milan, Italy"
    if any(term in title for term in ("italy", "italia")) or re.search(r"\b(?:in|across|within) italy\b", intro):
        return "Italy"
    return ""  # Unknown location: do not label this an Italy vacancy without evidence.


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

    elif provider == "teamtailor":
        # Teamtailor publishes a public RSS feed for its hosted careers sites.
        # This also works on custom careers domains such as careers.indigo.ai.
        # The official feed supports offset/per_page pagination without an API key.
        if not site or any(c in site for c in "/?&#"):
            raise ValueError("Teamtailor site must be a hostname, e.g. careers.indigo.ai")
        feed_url = f"https://{site}/jobs.rss"
        page_size = 100
        max_pages = 30
        ns = {"tt": "https://teamtailor.com/locations"}
        seen_ids = set()
        for page_number in range(max_pages):
            response = requests.get(
                feed_url,
                params={"offset": page_number * page_size, "per_page": page_size},
                headers={"User-Agent": "PersonalCareerMonitor/1.0"},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as error:
                raise ValueError("Teamtailor returned invalid RSS/XML") from error
            if root.tag != "rss" or root.find("channel") is None:
                raise ValueError("Teamtailor returned an unexpected RSS feed")
            items = root.findall("./channel/item")
            for item in items:
                job_id = (item.findtext("guid") or item.findtext("link") or "").strip()
                job_url = (item.findtext("link") or "").strip()
                if not job_id or not job_url or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                locations = []
                for location in item.findall("tt:locations/tt:location", ns):
                    city = (location.findtext("tt:city", default="", namespaces=ns) or "").strip()
                    country = (location.findtext("tt:country", default="", namespaces=ns) or "").strip()
                    label = ", ".join(part for part in [city, country] if part)
                    if label and label not in locations:
                        locations.append(label)
                results.append({
                    "id": job_id,
                    "title": (item.findtext("title") or "").strip(),
                    "location": "; ".join(locations),
                    "url": job_url,
                    "description": item.findtext("description") or "",
                })
            if len(items) < page_size:
                break
        else:
            raise ValueError("Teamtailor feed exceeded the configured page limit")

    elif provider == "contents":
        # Contents.com lists vacancies as accessible HTML accordions, not an ATS API.
        # Each role has a stable #job-... anchor. Never infer jobs from whole-page text.
        careers_url = company.get("careers_url") or "https://www.contents.com/careers/"
        response = requests.get(
            careers_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PersonalCareerMonitor/1.0)"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        parser = ContentsCareersParser()
        parser.feed(response.text)
        parser.close()
        if not parser.saw_accordion:
            raise ValueError("Contents.com careers page structure changed; job accordion not found")
        for item in parser.jobs:
            title = item["title"]
            job_id = item["id"]
            if not title or not job_id:
                continue
            description = " ".join(" ".join(item["description_parts"]).split())
            job_url = item["url"] or f"{careers_url.rstrip('/')}/#{job_id}"
            results.append({
                "id": job_id,
                "title": title,
                "location": contents_job_location(title, description),
                "url": urljoin(careers_url, job_url),
                "description": description,
            })

    elif provider == "stellantis":
        # Stellantis publishes a public JSONP search feed via Symphony Talent.
        # The request below was verified against its official careers site.
        # Scan the full public feed, then retain only jobs located in Italy.
        endpoint = "https://jobsapi-google.m-cloud.io/api/job/search"
        page_size = 100
        max_pages = 30
        italy_names = {"IT", "ITA", "ITALY", "ITALIA"}
        seen_ids = set()
        total_hits = None

        for page_number in range(max_pages):
            offset = page_number * page_size
            response = requests.get(
                endpoint,
                params={
                    "callback": "CWS.jobs.jobCallback",
                    "pageSize": page_size,
                    "offset": offset,
                    "companyName": site,
                    "orderBy": "posting_publish_time desc",
                    "queryLanguageCode": "en-US",
                    "disableSpellCheck": "true",
                    "enableBroadening": "true",
                },
                headers={"User-Agent": "Mozilla/5.0 (compatible; PersonalCareerMonitor/1.0)"},
                timeout=TIMEOUT,
            )
            response.raise_for_status()

            # The endpoint returns JSONP rather than plain JSON.
            body = response.text.strip().rstrip(";").strip()
            callback = "CWS.jobs.jobCallback("
            if not (body.startswith(callback) and body.endswith(")")):
                raise ValueError("Stellantis returned an unexpected JSONP response")
            payload = json.loads(body[len(callback):-1])
            page = payload.get("searchResults")
            total_hits = payload.get("totalHits")
            if not isinstance(page, list) or not isinstance(total_hits, int):
                raise ValueError("Stellantis returned an unexpected search response")
            if not page and offset < total_hits:
                raise ValueError("Stellantis pagination stopped before the full result set")

            for item in page:
                job = item.get("job") or {}
                job_id = job.get("id")
                job_url = job.get("url") or ""
                if job_id is None or not job_url or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                locations = job.get("google_locations") or []
                italian_locations = [
                    loc for loc in locations
                    if str(loc.get("country", "")).upper() in italy_names
                ]
                primary_country = str(job.get("primary_country") or "").upper()
                if primary_country not in italy_names and not italian_locations:
                    continue

                city_names = [str(loc.get("city") or "").strip() for loc in italian_locations]
                primary_city = str(job.get("primary_city") or "").strip()
                if primary_country in italy_names and primary_city:
                    city_names.insert(0, primary_city)
                city_names = list(dict.fromkeys(city for city in city_names if city))
                location = (", ".join(city_names) + ", Italy") if city_names else "Italy"

                results.append({
                    "id": str(job_id),
                    "title": job.get("title") or "",
                    "location": location,
                    "url": job_url,
                    "description": job.get("description") or "",
                })

            if offset + len(page) >= total_hits:
                break
        else:
            raise ValueError("Stellantis has more postings than the configured page limit")

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
