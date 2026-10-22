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


def miss(qid="m1", start=1000, duration=2000, subqueries=True, status="successful"):
    rec = sample_record(qid, subqueries=None if subqueries else [])
    rec.update({"optimization": [], "status": status, "startTime": start, "duration": duration,
                "modelId": "model-1"})
    return rec


class ClassifyTests(unittest.TestCase):
    def test_optimized(self):
        self.assertEqual(x.classify(sample_record(), 1, []), "optimized")

    def test_system_query(self):
        rec = miss()
        rec["queryType"] = "System"
        self.assertEqual(x.classify(rec, 1, []), "system")
        self.assertFalse(x.needs_aggregate_check(rec))

    def test_failed(self):
        self.assertEqual(x.classify(miss(status="failed"), 1, []), "failed")

    def test_no_warehouse(self):
        self.assertEqual(x.classify(miss(), 0, []), "no_warehouse")

    def test_aggregate_created_during_query(self):
        self.assertEqual(x.classify(miss(), 1, [2500.0]), "aggregate_created")

    def test_aggregate_created_shortly_after_query(self):
        self.assertEqual(x.classify(miss(), 1, [3000.0 + 9000]), "aggregate_created")

    def test_old_aggregate_does_not_count(self):
        self.assertEqual(x.classify(miss(start=100000), 1, [1000.0]), "no_aggregate_created")

    def test_unreadable_aggregates(self):
        self.assertEqual(x.classify(miss(), 1, None), "unclassified")

    def test_parse_ms(self):
        self.assertEqual(x.parse_ms("1970-01-01T00:00:01.500000Z"), 1500.0)
        self.assertEqual(x.parse_ms(None), 0)


# Minimal catalog export: Calendar and Store dimensions, FACT_SALES (both keys) and FACT_CHAIN
# (calendar key only, single-element collections written as bare objects like the real export).
EXPORT = {
    "attributes": {"keyed-attribute": [
        {"id": "a-year", "name": "Fiscal Year", "key-ref": "k-day"},
        {"id": "a-store", "name": "Store", "key-ref": "k-store"},
        {"id": "a-brand", "name": "Brand", "key-ref": "k-brand"}]},
    "data-sets": {"data-set": [{"id": "ds-sales", "name": "FACT_SALES"}, {"id": "ds-chain", "name": "FACT_CHAIN"}]},
    "dimensions": {"dimension": [
        {"name": "Calendar", "hierarchy": {"level": {"primary-attribute": "a-year"}},
         "calculation-group": {"name": "Time Relative"}},
        {"name": "Store Dim", "hierarchy": [{"level": {"primary-attribute": "a-store",
                                                       "keyed-attribute-ref": [{"attribute-id": "a-brand"}]}}]}]},
    "calculated-members": {"calculated-member": {"name": "Index", "expression": "[Measures].[Net Sales] / [Measures].[Chain Sales]"}},
    "cubes": {"cube": {"name": "sales_model", "properties": {"caption": "Sales Model"},
        "attributes": {"attribute": [{"id": "m-net", "name": "Net Sales"}, {"id": "m-chain", "name": "Chain Sales"}]},
        "data-sets": {"data-set-ref": [
            {"id": "ds-sales", "logical": {"key-ref": [{"id": "k-day"}, {"id": "k-store"}], "attribute-ref": {"id": "m-net"}}},
            {"id": "ds-chain", "logical": {"key-ref": {"id": "k-day"}, "attribute-ref": {"id": "m-chain"}}}]}}},
}


class UdaTests(unittest.TestCase):
    def setUp(self):
        self.index = x.catalog_index(EXPORT)["sales_model"]

    def uda(self, attributes, measures):
        return x.uda_sml({"modelName": "sales_model", "attributes": attributes, "measures": measures}, self.index)

    def test_index(self):
        self.assertEqual(self.index["attr_dims"]["Brand"], ["Store Dim"])
        self.assertEqual(self.index["metric_dataset"], {"Net Sales": "FACT_SALES", "Chain Sales": "FACT_CHAIN"})
        both = x.catalog_index(EXPORT)
        self.assertIs(both["Sales Model"], both["sales_model"])   # caption and name lead to the same index

    def test_calculation_split_per_fact_dataset(self):
        text = self.uda(["Fiscal Year", "Store"], ["Index"])
        self.assertIn("# left out Store for FACT_CHAIN: Store Dim has no relationship to it", text)
        self.assertEqual(text.count("  - unique_name: uda_"), 2)
        self.assertIn("    label: \"Suggested on FACT_CHAIN: Fiscal Year\"", text)
        self.assertIn("      - name: Store\n        dimension: Store Dim\n    metrics:\n      - Net Sales", text)

    def test_skips_calc_groups_helpers_and_unknown(self):
        text = self.uda(["Fiscal Year", "Time Relative", "Nope"], ["Net Sales", "__SUMATTRNAME__x", "Ghost"])
        self.assertIn("# skipped Time Relative: calculation group", text)
        self.assertIn("# skipped Nope: not found as a dimension attribute in the model", text)
        self.assertIn("# skipped Ghost: no base metric found in the model", text)
        self.assertNotIn("__SUMATTRNAME__", text)

    def test_dimension_only_query(self):
        self.assertTrue(self.uda(["Store"], []).startswith("# no UDA"))

    def test_same_footprint_same_name(self):
        self.assertEqual(self.uda(["Store"], ["Net Sales"]), self.uda(["Store"], ["Net Sales"]))

    def test_yaml_quoting(self):
        self.assertEqual(x.yaml_str("Net Sales"), "Net Sales")
        self.assertEqual(x.yaml_str("Sales: total"), '"Sales: total"')
        self.assertEqual(x.yaml_str("#1 Store"), '"#1 Store"')


class PollerTests(unittest.TestCase):
    def make_poller(self, pages):
        cfg = {"query_path": "/v1/query", "aggregates_path": "/v1/aggregates", "lookback_s": 1800,
               "page_size": 100, "max_pages": 5, "timeout_s": 5, "ssl": None}
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

    def test_paging_by_total(self):
        p1 = {"data": [{"stats": {"createdAt": "1970-01-01T00:00:02Z"}}], "total": 2}
        p2 = {"data": [{"stats": {"createdAt": "1970-01-01T00:00:03Z"}}], "total": 2}
        poller = self.make_poller([p1, p2])
        self.assertEqual(poller.aggregate_creation_times(["model-1"]), {"model-1": [2000.0, 3000.0]})

    def test_miss_is_classified_from_aggregates(self):
        history = {"results": [miss("new")], "hasNextPage": False}
        aggs = {"data": [{"stats": {"createdAt": "1970-01-01T00:00:01.200Z"}}], "total": 1}
        poller = self.make_poller([{"results": [], "hasNextPage": False}, history, aggs])
        poller.poll()
        with mock.patch.object(x, "record", wraps=x.record) as rec:
            poller.seen.clear()
            poller.get_json.side_effect = [history, aggs]
            poller.poll()
        self.assertEqual(rec.call_args[0][2], [1200.0])

    def test_unreadable_aggregates_do_not_fail_the_poll(self):
        history = {"results": [miss("new")], "hasNextPage": False}
        poller = self.make_poller([{"results": [], "hasNextPage": False}, history, OSError("503")])
        poller.poll()
        self.assertEqual(poller.poll(), 1)

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

    def test_page_size_capped_at_api_maximum(self):
        with mock.patch.dict(os.environ, {"PAGE_SIZE": "500"}, clear=True):
            self.assertEqual(x.load_config()["page_size"], 100)

    def test_aggregates_path_follows_query_path(self):
        with mock.patch.dict(os.environ, {"QUERY_PATH": "/v1/public/query"}, clear=True):
            self.assertEqual(x.load_config()["aggregates_path"], "/v1/public/aggregates")

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
        self.assertEqual(line["measures"], "")
        rec["attributes"], rec["measures"] = ["Store", "Fiscal Quarter"], ["Net Sales"]
        line = x.query_log_line(x.query_labels("prod", rec), rec, phases, count)
        self.assertEqual(line["attributes"], "Fiscal Quarter, Store")
        self.assertEqual(line["measures"], "Net Sales")
        self.assertNotIn("user", line)
        self.assertNotIn("userId", line)

    def test_metrics_render(self):
        x.record("render", sample_record("r1"))
        text = "\n".join(x.PHASE_HIST.render())
        self.assertIn('phase="warehouse"', text)
        self.assertIn('le="+Inf"', text)


if __name__ == "__main__":
    unittest.main()
