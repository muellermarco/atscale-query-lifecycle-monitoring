"""Unit tests for exporter.py. Sample code, provided as is (see DISCLAIMER.md).

Run: python3 -m unittest discover -s exporter
The records are synthetic and follow the shape of the AtScale query history API.
"""
import os
import unittest
from unittest import mock

import exporter as x


def sample_record(qid="q1", subqueries=None, events=True):
    rec = {"queryId": qid, "status": "successful", "queryType": "User", "catalogName": "Sales",
           "modelName": "Orders", "optimization": ["AGGS"], "dialect": "Snowflake",
           "startTime": 1000, "duration": 2148.0}
    if events:
        rec["events"] = [
            {"name": "Inbound Query", "startTime": 1000, "duration": 6.0},
            {"name": "Planning", "startTime": 1006, "duration": 41.0},
            {"name": "Outbound", "startTime": 1047, "duration": 2100.0,
             "subqueries": subqueries if subqueries is not None else
             [{"name": "Query 1", "startTime": 1700, "duration": 1440}]},
            {"name": "Result Processing", "startTime": 3147, "duration": 1.0},
        ]
    return rec


class PhaseTests(unittest.TestCase):
    def test_phases_and_outbound_split(self):
        phases, count = x.phase_durations(sample_record())
        self.assertEqual(count, 1)
        self.assertAlmostEqual(phases["inbound"], 0.006)
        self.assertAlmostEqual(phases["planning"], 0.041)
        self.assertAlmostEqual(phases["outbound"], 2.1)
        self.assertAlmostEqual(phases["warehouse"], 1.44)
        self.assertAlmostEqual(phases["outbound_wait"], 0.66)
        self.assertAlmostEqual(phases["result_processing"], 0.001)

    def test_parallel_subqueries_counted_once(self):
        subs = [{"startTime": 0, "duration": 100}, {"startTime": 50, "duration": 100},
                {"startTime": 300, "duration": 50}]
        self.assertEqual(x.interval_union_ms(subs), 200.0)

    def test_wait_never_negative(self):
        rec = sample_record(subqueries=[{"startTime": 1047, "duration": 5000}])
        phases, _ = x.phase_durations(rec)
        self.assertEqual(phases["outbound_wait"], 0.0)

    def test_unknown_phase_name_is_slugged(self):
        rec = sample_record()
        rec["events"].append({"name": "Cache Lookup", "duration": 2})
        phases, _ = x.phase_durations(rec)
        self.assertAlmostEqual(phases["cache_lookup"], 0.002)

    def test_record_without_events(self):
        self.assertFalse(x.record("test", sample_record(events=False)))

    def test_optimization_labels(self):
        rec = sample_record()
        rec["optimization"] = []
        self.assertEqual(x.query_labels("prod", rec)[5], "none")
        rec["optimization"] = ["CACHE", "AGGS"]
        self.assertEqual(x.query_labels("prod", rec)[5], "AGGS+CACHE")


class PollerTests(unittest.TestCase):
    def make_poller(self, pages):
        cfg = {"query_path": "/v1/query", "lookback_s": 1800, "page_size": 500, "max_pages": 5,
               "timeout_s": 5, "ssl": None}
        poller = x.InstancePoller("test", "https://atscale.example.com", "token", cfg)
        poller.get_json = mock.Mock(side_effect=pages)
        return poller

    def test_warm_up_skips_existing_then_counts_new_once(self):
        first = {"results": [sample_record("a")], "hasNextPage": False}
        second = {"results": [sample_record("a"), sample_record("b")], "hasNextPage": False}
        poller = self.make_poller([first, second, second])
        self.assertEqual(poller.poll(), 0)
        self.assertEqual(poller.poll(), 1)
        self.assertEqual(poller.poll(), 0)

    def test_paging(self):
        p1 = {"results": [sample_record("a")], "hasNextPage": True}
        p2 = {"results": [sample_record("b")], "hasNextPage": False}
        self.assertEqual(len(self.make_poller([p1, p2]).fetch()), 2)

    def test_unexpected_response_raises(self):
        with self.assertRaises(ValueError):
            self.make_poller([{"message": "not found"}]).fetch()


class ConfigTests(unittest.TestCase):
    def test_token_variable_name(self):
        self.assertEqual(x.token_var("prod-eu"), "ATSCALE_TOKEN_PROD_EU")

    def test_missing_token_fails_fast(self):
        env = {"ATSCALE_INSTANCES": "prod=https://atscale.example.com"}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            x.load_pollers({})

    def test_bad_url_fails_fast(self):
        env = {"ATSCALE_INSTANCES": "prod=atscale.example.com", "ATSCALE_TOKEN_PROD": "t"}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            x.load_pollers({})

    def test_query_log_line(self):
        rec = sample_record("log1")
        phases, count = x.phase_durations(rec)
        line = x.query_log_line(x.query_labels("prod", rec), rec, phases, count)
        self.assertEqual(line["event"], "atscale_query")
        self.assertEqual(line["query_id"], "log1")
        self.assertEqual(line["outbound_wait_ms"], 660.0)
        self.assertEqual(line["start"], "1970-01-01T00:00:01.000Z")
        self.assertNotIn("user", line)
        self.assertNotIn("userId", line)

    def test_metrics_render(self):
        x.record("render", sample_record("r1"))
        text = "\n".join(x.PHASE_HIST.render())
        self.assertIn('phase="warehouse"', text)
        self.assertIn('le="+Inf"', text)


if __name__ == "__main__":
    unittest.main()
