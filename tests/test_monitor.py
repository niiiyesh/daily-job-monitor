import unittest
from unittest.mock import Mock, patch

from monitor import fetch_jobs, matches


class JobMonitorTests(unittest.TestCase):
    def test_matching_junior_italy(self):
        config = {"title_keywords": ["data analyst"], "exclude_title_keywords": ["senior"], "locations": ["milan", "italy"]}
        self.assertTrue(matches({"title": "Junior Data Analyst", "location": "Milan, Italy"}, config))
        self.assertFalse(matches({"title": "Senior Data Analyst", "location": "Milan, Italy"}, config))
        self.assertFalse(matches({"title": "Junior Data Analyst", "location": "New York"}, config))
        self.assertFalse(matches({"title": "Designer", "location": "Milan, Italy"}, config))

    @patch("monitor.requests.get")
    def test_greenhouse_response(self, mocked_get):
        reply = Mock()
        reply.json.return_value = {"jobs": [{"id": 12, "title": "Business Analyst", "location": {"name": "Milan"}, "absolute_url": "https://example.org/j/12"}]}
        mocked_get.return_value = reply
        jobs = fetch_jobs({"name": "Example", "provider": "greenhouse", "site": "example"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "12")
        self.assertEqual(jobs[0]["location"], "Milan")

    @patch("monitor.requests.get")
    def test_ashby_response(self, mocked_get):
        reply = Mock()
        reply.json.return_value = {"jobs": [{"title":"Marketing Analyst", "location":"Milan", "jobUrl":"https://jobs.ashbyhq.com/company/xyz", "descriptionPlain":"English", "isListed":True}]}
        mocked_get.return_value = reply
        jobs = fetch_jobs({"name":"Satispay", "provider":"ashby", "site":"satispay"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Marketing Analyst")
        self.assertEqual(jobs[0]["location"], "Milan")

    @patch("monitor.requests.get")
    def test_workable_response(self, mocked_get):
        reply = Mock()
        reply.json.return_value = {"jobs": [{"shortcode":"XYZ", "title":"Data Analyst", "city":"Milan", "country":"Italy", "shortlink":"https://apply.workable.com/j/XYZ/"}]}
        mocked_get.return_value = reply
        jobs = fetch_jobs({"name":"Empatica", "provider":"workable", "site":"empatica"})
        self.assertEqual(jobs[0]["id"], "XYZ")
        self.assertEqual(jobs[0]["location"], "Milan, Italy")

    @patch("monitor.requests.get")
    def test_amazon_response_and_italy_filter(self, mocked_get):
        reply = Mock()
        reply.json.return_value = {"jobs": [
            {"id_icims": 123, "title": "Business Analyst", "location": "Milan, MI, ITA", "country_code": "ITA", "job_path": "/en/jobs/123/business-analyst"},
            {"id_icims": 999, "title": "Business Analyst", "location": "Paris, FRA", "country_code": "FRA", "job_path": "/en/jobs/999/business-analyst"}
        ]}
        mocked_get.return_value = reply
        jobs = fetch_jobs({"name": "Amazon / AWS", "provider": "amazon", "site": "italy"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "123")
        self.assertIn("Italy", jobs[0]["location"])
        self.assertEqual(jobs[0]["url"], "https://www.amazon.jobs/en/jobs/123/business-analyst")
        self.assertEqual(mocked_get.call_args.kwargs["params"]["normalized_country_code[]"], "ITA")

    @patch("monitor.requests.get")
    def test_amazon_pagination(self, mocked_get):
        first = Mock()
        first.json.return_value = {"jobs": [
            {"id_icims": i, "title": "Operations Analyst", "country_code": "ITA", "job_path": f"/en/jobs/{i}/role", "city": "Rome"}
            for i in range(100)
        ]}
        second = Mock()
        second.json.return_value = {"jobs": [
            {"id_icims": 100, "title": "Marketing Analyst", "country_code": "ITA", "job_path": "/en/jobs/100/role", "city": "Milan"}
        ]}
        mocked_get.side_effect = [first, second]
        jobs = fetch_jobs({"name": "Amazon / AWS", "provider": "amazon", "site": "italy"})
        self.assertEqual(len(jobs), 101)
        self.assertEqual(mocked_get.call_count, 2)

    @patch("monitor.requests.get")
    def test_stellantis_pagination_and_italy_only(self, mocked_get):
        def make_reply(jobs, hits):
            reply = Mock()
            reply.text = 'CWS.jobs.jobCallback(' + __import__('json').dumps({
                'totalHits': hits, 'searchResults': [{'job': job} for job in jobs]
            }) + ');'
            return reply

        first_page = [
            {'id': 1001, 'title': 'Project Analyst', 'primary_country': 'IT',
             'primary_city': 'Torino', 'url': 'https://careers.stellantis.com/job/1001/',
             'description': 'Early career project management'}
        ] + [
            {'id': i, 'title': 'Engineering role', 'primary_country': 'US',
             'url': f'https://careers.stellantis.com/job/{i}/'}
            for i in range(2000, 2099)
        ]
        second_page = [{
            'id': 1002, 'title': 'Marketing Internship', 'primary_country': 'US',
            'google_locations': [{'city':'Milano','country':'IT'}],
            'url': 'https://careers.stellantis.com/job/1002/'
        }]
        mocked_get.side_effect = [make_reply(first_page, 101), make_reply(second_page, 101)]
        jobs = fetch_jobs({
            'name': 'Stellantis', 'provider': 'stellantis',
            'site': 'companies/16115603-6c1b-4c45-b544-238a4e6c51b3'
        })
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]['location'], 'Torino, Italy')
        self.assertEqual(jobs[1]['location'], 'Milano, Italy')
        self.assertEqual(mocked_get.call_count, 2)
        self.assertEqual(mocked_get.call_args.kwargs['params']['offset'], 100)

    @patch("monitor.requests.get")
    def test_stellantis_bad_response_does_not_look_like_zero_jobs(self, mocked_get):
        mocked_get.return_value.text = '<html>Unavailable</html>'
        with self.assertRaisesRegex(ValueError, 'JSONP'):
            fetch_jobs({
                'name': 'Stellantis', 'provider': 'stellantis',
                'site': 'companies/16115603-6c1b-4c45-b544-238a4e6c51b3'
            })

    @patch("monitor.requests.get")
    def test_teamtailor_rss_and_italy_location(self, mocked_get):
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
        <title>indigo.ai</title>
        <item><title>AI Implementation Intern - Milan</title>
        <guid>job-123</guid><link>https://careers.indigo.ai/jobs/123</link>
        <description>Apply today</description>
        <tt:locations><tt:location><tt:city>Milano</tt:city><tt:country>Italy</tt:country></tt:location></tt:locations>
        </item>
        </channel></rss>'''
        mocked_get.return_value.content = xml
        jobs = fetch_jobs({"name": "indigo.ai", "provider": "teamtailor", "site": "careers.indigo.ai"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "job-123")
        self.assertEqual(jobs[0]["location"], "Milano, Italy")
        self.assertEqual(jobs[0]["url"], "https://careers.indigo.ai/jobs/123")
        self.assertIn("jobs.rss", mocked_get.call_args.args[0])
        self.assertTrue(matches(jobs[0], {
            "title_keywords": ["intern"],
            "exclude_title_keywords": ["senior"],
            "locations": ["italy"]
        }))

    @patch("monitor.requests.get")
    def test_teamtailor_rejects_invalid_feed(self, mocked_get):
        mocked_get.return_value.content = b"<html><body>Not a jobs feed</body></html>"
        with self.assertRaisesRegex(ValueError, "RSS feed"):
            fetch_jobs({"name": "indigo.ai", "provider": "teamtailor", "site": "careers.indigo.ai"})

    @patch("monitor.requests.get")
    def test_contents_page_finds_italy_roles_and_skips_doha(self, mocked_get):
        html = """<html><div id="job_accordion">
        <div id="job-account-executive-italy-europe-uk" class="level">
            <a class="opening" title="Account Executive – Italy, Europe &amp; UK"><span>Account Executive</span></a>
            <div class="expanded pos_0">
              <a href="https://www.contents.com/careers/#job-account-executive-italy-europe-uk" class="perma-to-job-opening">Apply</a>
              <p>We are seeking an Account Executive with a base in Milan.</p>
              <form><input type="text"></form>
            </div>
        </div>
        <div id="job-sales-gcc" class="level">
            <a class="opening" title="Sales Executive – GCC Market (Doha Hub)"></a>
            <div class="expanded"><p>Based in Doha.</p></div>
        </div>
        <div id="job-business-development" class="level">
            <a class="opening" title="Business development Manager - content &amp; creative services"></a>
            <div class="expanded"><p>Support our growth across Italy and selected international markets.</p></div>
        </div></div></html>"""
        mocked_get.return_value.text = html
        jobs = fetch_jobs({"name":"Contents.com", "provider":"contents", "site":"contents.com", "careers_url":"https://www.contents.com/careers/"})
        self.assertEqual(len(jobs), 3)
        self.assertEqual([job["location"] for job in jobs], ["Milan, Italy", "Doha, Qatar", "Italy"])
        self.assertEqual(jobs[0]["id"], "job-account-executive-italy-europe-uk")
        self.assertEqual(jobs[1]["url"], "https://www.contents.com/careers/#job-sales-gcc")
        config = {"title_keywords":["account executive", "sales executive", "business development"],
                  "exclude_title_keywords":["senior"], "locations":["italy","milan"]}
        self.assertEqual([job["title"] for job in jobs if matches(job,config)],
                         ["Account Executive – Italy, Europe & UK", "Business development Manager - content & creative services"])

    @patch("monitor.requests.get")
    def test_contents_structure_change_fails_loudly(self, mocked_get):
        mocked_get.return_value.text = '<html><body>New page</body></html>'
        with self.assertRaisesRegex(ValueError, "accordion not found"):
            fetch_jobs({"name":"Contents.com", "provider":"contents", "site":"contents.com"})

    @patch("monitor.requests.post")
    def test_workday_italy_facet_and_pagination(self, mocked_post):
        def page(data):
            reply = Mock()
            reply.json.return_value = data
            return reply
        location_facets = [{
            "facetParameter": "locationMainGroup", "values": [{
                "facetParameter": "locationCountry", "values": [
                    {"descriptor": "Italy", "id": "country-IT", "count": 2},
                    {"descriptor": "France", "id": "country-FR", "count": 4},
                ]
            }]
        }]
        mocked_post.side_effect = [
            page({"total": 6, "jobPostings": [], "facets": location_facets}),
            page({"total": 2, "jobPostings": [
                {"title": "Junior Business Analyst", "externalPath": "/job/Milan/Business-Analyst_R123", "locationsText": "Milan", "bulletFields": ["R123"]},
                {"title": "Digital Marketing Intern", "externalPath": "/job/Rome/Marketing-Intern_R124", "locationsText": "Rome", "bulletFields": ["R124"]},
            ]}),
        ]
        jobs = fetch_jobs({"name": "dentsu / Merkle", "provider": "workday", "site": "dentsuaegis"})
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "R123")
        self.assertEqual(jobs[0]["location"], "Milan, Italy")
        self.assertEqual(jobs[1]["url"], "https://dentsuaegis.wd3.myworkdayjobs.com/en-US/DAN_GLOBAL/job/Rome/Marketing-Intern_R124")
        self.assertEqual(mocked_post.call_count, 2)
        self.assertEqual(mocked_post.call_args.kwargs["json"]["appliedFacets"], {"locationCountry": ["country-IT"]})

    @patch("monitor.requests.post")
    def test_workday_no_italy_facet_is_zero(self, mocked_post):
        mocked_post.return_value.json.return_value = {
            "total": 1, "jobPostings": [{"title": "Designer", "locationsText": "Paris"}],
            "facets": [{"facetParameter": "locationMainGroup", "values": [
                {"facetParameter": "locationCountry", "values": [{"descriptor": "France", "id": "fr", "count": 1}]}
            ]}],
        }
        jobs = fetch_jobs({"name": "dentsu / Merkle", "provider": "workday", "site": "dentsuaegis"})
        self.assertEqual(jobs, [])
        self.assertEqual(mocked_post.call_count, 1)

    @patch("monitor.requests.post")
    def test_workday_missing_facets_fail_instead_of_silently_ignoring_jobs(self, mocked_post):
        mocked_post.return_value.json.return_value = {"total": 10, "jobPostings": [], "facets": []}
        with self.assertRaisesRegex(ValueError, "country facets"):
            fetch_jobs({"name": "dentsu / Merkle", "provider": "workday", "site": "dentsuaegis"})

    @patch("monitor.requests.get")
    def test_lever_response(self, mocked_get):
        reply = Mock()
        reply.json.return_value = [{"id": "abc", "text": "Marketing Analyst", "categories": {"allLocations": ["Milano"]}, "hostedUrl": "https://example.org/j/abc"}]
        mocked_get.return_value = reply
        jobs = fetch_jobs({"name": "Example", "provider": "lever", "site": "example"})
        self.assertEqual(jobs[0]["title"], "Marketing Analyst")
        self.assertEqual(jobs[0]["location"], "Milano")


if __name__ == "__main__":
    unittest.main()
