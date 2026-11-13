import csv
from datetime import datetime
from io import StringIO
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import daily_digest


COMPANY_CONFIG = {
    "title_keywords": ["analyst", "founder associate"],
    "exclude_title_keywords": ["senior"],
    "locations": ["italy", "milan"],
    "companies": [
        {"name": "GoodCo", "provider": "ashby", "site": "good"},
        {"name": "BadCo", "provider": "workday", "site": "bad"},
    ],
}


class DailyDigestTests(unittest.TestCase):
    @patch("daily_digest.fetch_jobs")
    def test_filtering_and_source_failure_not_called_zero(self, mocked):
        def results(company):
            if company["name"] == "BadCo":
                raise ValueError("Bad feed")
            return [
                {"id": "1", "title": "Marketing Analyst", "location": "Milan, Italy", "url": "https://example.com/1"},
                {"id": "2", "title": "Senior Analyst", "location": "Milan, Italy", "url": "https://example.com/2"},
                {"id": "3", "title": "Marketing Analyst", "location": "London", "url": "https://example.com/3"},
                {"id": "1", "title": "Marketing Analyst", "location": "Milan, Italy", "url": "https://example.com/1"},
            ]
        mocked.side_effect = results
        groups = daily_digest.collect_daily_jobs(COMPANY_CONFIG)
        self.assertEqual(len(groups[0]["jobs"]), 1)
        self.assertEqual(groups[0]["total"], 4)
        self.assertTrue(groups[1]["error"])
        msg = daily_digest.make_message(groups, datetime(2026, 11, 12, tzinfo=ZoneInfo("Europe/Rome")))
        self.assertIn("1 matching listings", msg)
        self.assertIn("source unavailable", msg)

    @patch("daily_digest.fetch_jobs")
    def test_all_sources_failed_does_not_send_misleading_summary(self, mocked):
        mocked.side_effect = ValueError("feed broken")
        with self.assertRaisesRegex(RuntimeError, "All career sources failed"):
            daily_digest.collect_daily_jobs(COMPANY_CONFIG)

    def test_csv_has_every_match_with_links_and_stale_warning(self):
        groups = [{"name": "Stellantis — Turin listings (supplement)", "provider": "stellantis_torino",
                   "total": 2, "error": False, "source_last_updated": "2026-10-12",
                   "jobs": [{"title": "Founder Associate", "location": "Turin, Italy",
                             "id": "x", "url": "https://example.com/x"},
                            {"title": "Junior Analyst", "location": "Turin, Italy",
                             "id": "y", "url": "https://example.com/y"}]}]
        raw = daily_digest.make_csv(groups).decode("utf-8-sig")
        rows = list(csv.DictReader(StringIO(raw)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Application link"], "https://example.com/x")
        self.assertIn("Verify", rows[0]["Caution"])
        self.assertEqual(rows[1]["Source last updated"], "2026-10-12")

    def test_csv_injection_guard(self):
        self.assertEqual(daily_digest._cell("=SUM(1,2)"), "'=SUM(1,2)")
        self.assertEqual(daily_digest._cell("@command"), "'@command")

    @patch("daily_digest.requests.post")
    def test_telegram_summary_and_csv_sent_separately(self, mocked):
        mocked.return_value.status_code = 200
        mocked.return_value.json.return_value = {"ok": True}
        day = datetime(2026, 11, 12, tzinfo=ZoneInfo("Europe/Rome"))
        groups = [{"name": "Example", "provider": "greenhouse", "total": 1,
                   "jobs": [{"title": "Business Analyst", "id": "1", "location": "Milan, Italy",
                             "url": "https://example.com/job/1"}], "error": False, "source_last_updated": ""}]
        daily_digest.send_daily_digest(groups, "fake-token", "123", day)
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("sendMessage", mocked.call_args_list[0].args[0])
        self.assertIn("sendDocument", mocked.call_args_list[1].args[0])
        file_tuple = mocked.call_args_list[1].kwargs["files"]["document"]
        self.assertEqual(file_tuple[0], "matching_jobs_2026-11-12.csv")
        self.assertIn(b"Business Analyst", file_tuple[1])

    @patch("daily_digest.requests.post")
    def test_reject_missing_credentials_without_sending(self, mocked):
        with self.assertRaisesRegex(RuntimeError, "Missing"):
            daily_digest.send_daily_digest([], "", "", datetime(2026, 11, 12))
        mocked.assert_not_called()

    @patch("daily_digest.requests.post")
    def test_failed_telegram_does_not_print_secret(self, mocked):
        mocked.return_value.status_code = 401
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as ctx:
            daily_digest._telegram_post("secret-abc", "sendMessage", json={"chat_id": "1", "text": "Hi"})
        self.assertNotIn("secret-abc", str(ctx.exception))

    def test_summary_never_exceeds_telegram_limit(self):
        groups = [{"name": "Example Corp " + str(i), "provider": "ashby", "total": 20,
                   "jobs": [{"title": "Business Analyst " + "X" * 200}],
                   "error": False, "source_last_updated": ""}
                  for i in range(100)]
        msg = daily_digest.make_message(groups, datetime(2026, 11, 12))
        self.assertLessEqual(len(msg), 4096)


if __name__ == "__main__":
    unittest.main()


class FreshVacanciesTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "title_keywords": ["analyst", "intern"],
            "exclude_title_keywords": ["senior"],
            "locations": ["italy"],
            "companies": [
                {"name": "Stellantis — Turin listings (supplement)",
                 "provider": "stellantis_torino", "site": "torino"},
                {"name": "Satispay", "provider": "ashby", "site": "satispay"},
            ],
        }

    @patch("daily_digest.fetch_jobs")
    def test_outdated_turin_jobs_are_absent_from_message_and_csv(self, mocked):
        """Curated Oct 12 links must NOT appear as current matches on Nov 13."""
        def fetch(company):
            if company["provider"] == "stellantis_torino":
                return [{"id": "linkedin-12345", "title": "Marketing Intern",
                         "location": "Torino, Italy", "url": "https://www.linkedin.com/jobs/view/12345/",
                         "source_last_updated": "2026-10-12"}]
            return [{"id": "new-job", "title": "Business Analyst",
                     "location": "Milan, Italy", "url": "https://jobs.ashbyhq.com/satispay/new-job"}]
        mocked.side_effect = fetch
        groups = daily_digest.collect_daily_jobs(self.config, checked_on=datetime(2026, 11, 13).date())
        self.assertEqual(groups[0]["excluded_stale"], 1)
        self.assertTrue(groups[0]["source_outdated"])
        self.assertEqual(groups[0]["jobs"], [])
        self.assertEqual(len(groups[1]["jobs"]), 1)
        self.assertIn("outdated/unverified matching jobs excluded", daily_digest.make_message(groups, datetime(2026,11,13)))
        csv_data = daily_digest.make_csv(groups).decode("utf-8-sig")
        self.assertNotIn("linkedin.com/jobs/view/12345", csv_data)
        self.assertIn("jobs.ashbyhq.com/satispay/new-job", csv_data)

    @patch("daily_digest.fetch_jobs")
    def test_recent_turin_list_kept_with_caution(self, mocked):
        mocked.return_value = [{"id": "3", "title": "Marketing Intern",
                                 "location": "Torino, Italy", "url": "https://www.linkedin.com/jobs/view/3/",
                                 "source_last_updated": "2026-11-10"}]
        groups = daily_digest.collect_daily_jobs({**self.config, "companies": self.config["companies"][:1]},
                                                 checked_on=datetime(2026, 11, 13).date())
        self.assertFalse(groups[0]["source_outdated"])
        self.assertEqual(len(groups[0]["jobs"]), 1)
        csv_data = daily_digest.make_csv(groups).decode("utf-8-sig")
        self.assertIn("posting may be closed", csv_data)

    @patch("daily_digest.fetch_jobs")
    def test_undated_turin_list_is_not_presented_as_current(self, mocked):
        mocked.return_value = [{"id": "3", "title": "Marketing Intern",
                                 "location": "Torino, Italy", "url": "https://www.linkedin.com/jobs/view/3/"}]
        groups = daily_digest.collect_daily_jobs({**self.config, "companies": self.config["companies"][:1]},
                                                 checked_on=datetime(2026,11,13).date())
        self.assertEqual(groups[0]["jobs"], [])
        self.assertEqual(groups[0]["excluded_stale"], 1)

    def test_age_validation(self):
        today = datetime(2026, 11, 13).date()
        self.assertTrue(daily_digest._supplement_is_recent("2026-10-30", today, 14))
        self.assertFalse(daily_digest._supplement_is_recent("2026-10-29", today, 14))
        self.assertFalse(daily_digest._supplement_is_recent("2026-11-14", today, 14))
        self.assertFalse(daily_digest._supplement_is_recent("", today, 14))
        self.assertFalse(daily_digest._supplement_is_recent("not a date", today, 14))
        for bad in [-1, 366, "14", True]:
            with self.assertRaises(ValueError):
                daily_digest.collect_daily_jobs({**self.config, "max_supplement_age_days": bad}, checked_on=today)
