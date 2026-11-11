"""Check public ATS job boards and alert on newly published matching roles."""

import argparse
import hashlib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
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


class DittaCareersParser(HTMLParser):
    """Collect open roles from the official Ditta careers page.

    Ditta renders its vacancies as HTML <details class="ruolo"> elements
    within a <div data-roles> container. Only summary titles and locations
    are used; application forms, page navigation and spontaneous applications
    are not treated as separate jobs.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.saw_roles = False
        self.current = None
        self.in_summary = False
        self.span_stack = []
        self.capture = None
        self.jobs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "data-roles" in attrs:
            self.saw_roles = True
        if tag == "details" and self.saw_roles and "ruolo" in (attrs.get("class") or "").split():
            self.current = {"title_parts": [], "location_parts": []}
        elif tag == "summary" and self.current is not None:
            self.in_summary = True
        elif tag == "span" and self.in_summary:
            self.span_stack.append(self.capture)
            classes = (attrs.get("class") or "").split()
            if "font-semibold" in classes:
                self.capture = "title_parts"
            elif "text-ink-dim" in classes:
                self.capture = "location_parts"

    def handle_data(self, data):
        if self.current is not None and self.in_summary and self.capture:
            self.current[self.capture].append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self.in_summary and self.span_stack:
            self.capture = self.span_stack.pop()
        elif tag == "summary":
            self.in_summary = False
            self.capture = None
            self.span_stack = []
        elif tag == "details" and self.current is not None:
            title = " ".join(" ".join(self.current["title_parts"]).split())
            location = " ".join(" ".join(self.current["location_parts"]).split())
            if title and location:
                self.jobs.append((title, location))
            self.current = None
            self.in_summary = False
            self.capture = None
            self.span_stack = []


def ditta_normalize_location(location):
    """Label only explicitly Italian cities/country to support Italy filters."""
    loc = location.strip()
    lower = loc.casefold()
    cities = ("milan", "milano", "florence", "firenze", "rome", "roma", "turin", "torino", "bologna", "naples", "napoli")
    if "italy" in lower or "italia" in lower:
        return loc
    if any(city in lower for city in cities):
        return f"{loc}, Italy"
    return loc


class StellantisTorinoParser(HTMLParser):
    """Extract Stellantis job links curated by the City of Turin.

    These are *supplementary* listings, not a live official Stellantis job feed.
    Only LinkedIn vacancy links inside the page's article are accepted.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.article_depth = 0
        self.saw_article = False
        self.active_id = None
        self.active_title = []
        self.jobs = []
        self.seen_ids = set()
        self.updated = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article":
            self.article_depth += 1
            self.saw_article = True
        if not self.article_depth:
            return
        if tag == "time" and not self.updated:
            stamp = (attrs.get("datetime") or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
                self.updated = stamp
        if tag != "a":
            return
        href = attrs.get("href") or ""
        parsed = urlparse(href)
        if parsed.hostname not in {"www.linkedin.com", "linkedin.com", "it.linkedin.com"}:
            return
        found = re.fullmatch(r"/jobs/view/(\d{7,})/?", parsed.path)
        if found:
            self.active_id = found.group(1)
            self.active_title = []

    def handle_data(self, data):
        if self.active_id is not None:
            self.active_title.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active_id is not None:
            title = " ".join(" ".join(self.active_title).split())
            if title and self.active_id not in self.seen_ids:
                self.seen_ids.add(self.active_id)
                self.jobs.append({
                    "id": f"linkedin-{self.active_id}",
                    "title": title,
                    "location": "Torino area, Italy",
                    "url": f"https://www.linkedin.com/jobs/view/{self.active_id}/",
                    "description": "",
                })
            self.active_id = None
            self.active_title = []
        if tag == "article" and self.article_depth:
            self.article_depth -= 1


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


    elif provider == "ditta":
        # Ditta publishes vacancies in server-rendered HTML summary elements.
        careers_url = company.get("careers_url") or "https://ditta.io/en/careers/"
        response = requests.get(
            careers_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PersonalCareerMonitor/1.0)"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        parser = DittaCareersParser()
        parser.feed(response.text)
        parser.close()
        if not parser.saw_roles:
            raise ValueError("Ditta careers page structure changed: jobs container missing")
        if not parser.jobs:
            # Avoid silently treating a changed site structure as zero vacancies.
            raise ValueError("Ditta careers page returned no recognizable positions")
        for title, location in parser.jobs:
            stable_key = f"{title.casefold()}|{location.casefold()}"
            job_id = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:20]
            results.append({
                "id": job_id,
                "title": title,
                "location": ditta_normalize_location(location),
                "url": f"{careers_url.rstrip('/')}/#posizioni",
                "description": "",
            })

    elif provider == "workday":
        # dentsu/Merkle uses the official Workday CXS jobs API. Query its
        # locationCountry facets, then fetch ALL pages of the Italy facet.
        # This avoids making guesses about city names or language requirements.
        base_url = f"https://{site}.wd3.myworkdayjobs.com"
        careers_id = company.get("workday_careers_id", "DAN_GLOBAL")
        tenant = company.get("workday_tenant", site)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", site) or not re.fullmatch(r"[A-Za-z0-9_-]+", tenant) or not re.fullmatch(r"[A-Za-z0-9_-]+", careers_id):
            raise ValueError("Invalid Workday site or tenant identifier")
        api_url = f"{base_url}/wday/cxs/{tenant}/{careers_id}/jobs"
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "PersonalCareerMonitor/1.0"}
        page_size = 20
        def search_workday(offset, facets):
            response = requests.post(
                api_url,
                json={"appliedFacets": facets, "limit": page_size, "offset": offset, "searchText": ""},
                headers=headers, timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("total"), int) or not isinstance(payload.get("jobPostings"), list):
                raise ValueError("Workday returned an unexpected jobs response")
            return payload

        initial = search_workday(0, {})
        locations_group = next((x for x in initial.get("facets", []) if x.get("facetParameter") == "locationMainGroup"), None)
        if not isinstance(locations_group, dict):
            raise ValueError("Workday country facets are missing; cannot verify Italy vacancies")
        country_group = next((x for x in locations_group.get("values", []) if x.get("facetParameter") == "locationCountry"), None)
        if not isinstance(country_group, dict):
            raise ValueError("Workday country filter is missing; refusing unverified jobs")
        italy = next((x for x in country_group.get("values", []) if str(x.get("descriptor", "")).casefold() in ("italy", "italia")), None)
        if italy is None:
            # Verified country facet has no Italy openings right now.
            return []
        if not italy.get("id"):
            raise ValueError("Workday Italy facet ID is missing")
        italy_facets = {"locationCountry": [italy["id"]]}
        seen_ids = set()
        for offset in range(0, 10000, page_size):
            payload = search_workday(offset, italy_facets)
            page = payload["jobPostings"]
            total = payload["total"]
            if offset < total and not page:
                raise ValueError("Workday returned incomplete Italy pagination")
            for job in page:
                path = str(job.get("externalPath") or "")
                if not path.startswith("/job/"):
                    continue
                job_id = (job.get("bulletFields") or [None])[0] or path
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                place = (job.get("locationsText") or "").strip()
                location = f"{place}, Italy" if place and "location" not in place.casefold() else "Italy"
                results.append({
                    "id": str(job_id),
                    "title": job.get("title") or "",
                    "location": location,
                    "url": f"{base_url}/en-US/{careers_id}{path}",
                    "description": "",  # job listing API has no full description; titles drive filters.
                })
            if offset + len(page) >= total:
                break
        else:
            raise ValueError("Workday Italy results exceeded the configured page limit")


    elif provider == "stellantis_torino":
        # The official Stellantis international API currently has no Italy posts.
        # As a supplement, monitor vacancies curated by the City of Turin.
        # This is not a complete or real-time inventory of Stellantis Italy.
        careers_url = company.get("careers_url") or (
            "https://www.torinogiovani.it/index.php/lavoro/ricerca-del-lavoro/"
            "offerte-di-lavoro-stellantis"
        )
        response = requests.get(
            careers_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PersonalCareerMonitor/1.0)"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        parser = StellantisTorinoParser()
        parser.feed(response.text)
        parser.close()
        if not parser.saw_article:
            raise ValueError("City of Turin vacancy page structure changed: article missing")
        if not parser.jobs:
            raise ValueError("City of Turin vacancy page has no recognizable LinkedIn job links")
        for item in parser.jobs:
            # Source date documents the curation date, not the posting date.
            item["source_last_updated"] = parser.updated
            results.append(item)

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
        if company["provider"].lower() == "stellantis_torino":
            updated = next((job.get("source_last_updated") for job in jobs if job.get("source_last_updated")), "unknown")
            print(f"  Supplementary City of Turin list, last updated: {updated}. "
                  "Jobs may be stale; confirm each vacancy is still open.")
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
