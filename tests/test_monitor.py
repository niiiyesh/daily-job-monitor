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
    def test_lever_response(self, mocked_get):
        reply = Mock()
        reply.json.return_value = [{"id": "abc", "text": "Marketing Analyst", "categories": {"allLocations": ["Milano"]}, "hostedUrl": "https://example.org/j/abc"}]
        mocked_get.return_value = reply
        jobs = fetch_jobs({"name": "Example", "provider": "lever", "site": "example"})
        self.assertEqual(jobs[0]["title"], "Marketing Analyst")
        self.assertEqual(jobs[0]["location"], "Milano")


if __name__ == "__main__":
    unittest.main()
