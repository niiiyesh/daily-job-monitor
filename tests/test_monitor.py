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
    def test_lever_response(self, mocked_get):
        reply = Mock()
        reply.json.return_value = [{"id": "abc", "text": "Marketing Analyst", "categories": {"allLocations": ["Milano"]}, "hostedUrl": "https://example.org/j/abc"}]
        mocked_get.return_value = reply
        jobs = fetch_jobs({"name": "Example", "provider": "lever", "site": "example"})
        self.assertEqual(jobs[0]["title"], "Marketing Analyst")
        self.assertEqual(jobs[0]["location"], "Milano")


if __name__ == "__main__":
    unittest.main()
