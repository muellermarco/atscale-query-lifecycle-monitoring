"""Generate the Elasticsearch variant of the AtScale query lifecycle dashboard.

Sample code, provided as is under the MIT License. Not an AtScale product. See DISCLAIMER.md.

Purpose: same dashboard as generate.py for sites that keep metrics in Prometheus and logs in Elasticsearch.
The Prometheus panels are taken unchanged from generate.py; every Loki panel is replaced, at the same
position, by a panel on the Grafana Elasticsearch data source. The log fields come from the ingest pipelines
in deploy/elastic/ (atscale.* fields, buckets and join keys are computed at ingest time).
Inputs: generate.py. Outputs: dashboard/atscale-query-lifecycle-elasticsearch.json and the Helm chart copy.
Run: python3 dashboard/generate_elastic.py
"""
import copy
import json
from pathlib import Path

import generate as g

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = [ROOT / "dashboard" / "atscale-query-lifecycle-elasticsearch.json",
           ROOT / "deploy" / "helm" / "atscale-query-lifecycle" / "files" / "atscale-query-lifecycle-elasticsearch.json"]

ES_ENG = {"type": "elasticsearch", "uid": "${es_engine}"}
ES_EXP = {"type": "elasticsearch", "uid": "${es_exporter}"}
ENG_FILTER = "($engine_filter)"
EXP_FILTER = ('atscale.env:${env:lucene} AND atscale.query_type:${query_type:lucene} AND atscale.catalog:${catalog:lucene} '
              'AND atscale.model:${model:lucene}')
CAND = 'atscale.event:aggregate_candidate AND atscale.score:[$min_score TO $max_score] AND ' + ENG_FILTER
QUEUE = "atscale.event:queue AND " + ENG_FILTER
ES_NOTE = (" Read from Elasticsearch (data streams logs-atscale.engine / logs-atscale.exporter, fields from the ingest "
           "pipelines in deploy/elastic).")


def terms(i, field, size=50):
    return {"id": str(i), "type": "terms", "field": field,
            "settings": {"min_doc_count": "1", "order": "desc", "orderBy": "_count", "size": str(size)}}


def date_hist(i):
    return {"id": str(i), "type": "date_histogram", "field": "@timestamp",
            "settings": {"interval": "auto", "min_doc_count": "0", "trimEdges": "0"}}


def tgt(ds, ref, query, metrics, buckets=(), alias=""):
    return {"datasource": ds, "refId": ref, "query": query, "alias": alias, "timeField": "@timestamp",
            "metrics": [dict(m, id=str(n + 1)) for n, m in enumerate(metrics)], "bucketAggs": list(buckets)}


def raw(ds, ref, query, size=1000):
    return tgt(ds, ref, query, [{"type": "raw_data", "settings": {"size": str(size)}}])


def strip_prefix():
    """Rename atscale.x to x, so the transformations of generate.py apply unchanged."""
    return {"id": "renameByRegex", "options": {"regex": "^atscale\\.(.*)$", "renamePattern": "$1"}}


def es_slowest(p):
    p["datasource"] = ES_EXP
    p["targets"] = [raw(ES_EXP, "A", "atscale.event:atscale_query AND " + EXP_FILTER +
                        " AND atscale.optimization:${optimization:lucene} AND atscale.status:${status:lucene}", 1000)]
    fields = ["atscale." + f for f in g.LOG_FIELDS]
    p["transformations"] = [{"id": "filterFieldsByName", "options": {"include": {"names": fields}}}, strip_prefix()] + \
        [t for t in p["transformations"] if t["id"] in ("organize", "sortBy")]
    for o in p["fieldConfig"]["overrides"]:
        for prop in o["properties"]:
            if prop["id"] == "links":
                prop["value"] = [{"title": "Show query text", "url": QUERY_TEXT_LINK},
                                 {"title": "Show trace (Tempo)", "url": g.TRACE_LINK, "targetBlank": True}]


def es_query_text(p):
    p["datasource"] = ES_ENG
    p["targets"] = [tgt(ES_ENG, "A", 'atscale.query_id:"$query_id" AND atscale.event:(inbound OR outbound) AND ' + ENG_FILTER,
                        [{"type": "logs", "settings": {"limit": "200"}}])]


def es_queue_for_query(p):
    q = 'atscale.query_id:"$query_id" AND ' + QUEUE
    p["datasource"] = ES_ENG
    p["targets"] = [tgt(ES_ENG, "A", q, [{"type": "count"}, {"type": "max", "field": "atscale.queue_position"}],
                        [terms(10, "atscale.connection")]),
                    tgt(ES_ENG, "B", q + " AND atscale.queue_position:>1", [{"type": "cardinality", "field": "atscale.request_id"}],
                        [terms(10, "atscale.connection")])]
    p["transformations"] = [{"id": "merge", "options": {}}, {"id": "organize", "options": {
        "renameByName": {"atscale.connection": "Connection", "Count": "Warehouse queries",
                         "Unique Count": "Queued behind others", "Max": "Deepest place"},
        "indexByName": {"atscale.connection": 0, "Count": 1, "Unique Count": 2, "Max": 3}}}]


def es_candidates_by_grain(p):
    p["datasource"] = ES_ENG
    by = [terms(10, "atscale.dataset"), terms(11, "atscale.grain", 200)]
    p["targets"] = [tgt(ES_ENG, "A", CAND, [{"type": "count"}, {"type": "avg", "field": "atscale.score"}], by),
                    tgt(ES_ENG, "B", CAND + " AND atscale.score:<$compression_threshold",
                        [{"type": "cardinality", "field": "atscale.candidate"}], by)]
    p["transformations"] = [{"id": "merge", "options": {}}, {"id": "organize", "options": {
        "renameByName": {"atscale.dataset": "Fact dataset", "atscale.grain": "Aggregate grain", "Count": "Candidates",
                         "Unique Count": "Below threshold", "Average": "Compression score"},
        "indexByName": {"atscale.dataset": 0, "atscale.grain": 1, "Count": 2, "Unique Count": 3,
                        "Average": 4}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Compression score"}]}}]


def es_candidates_below(p):
    p["datasource"] = ES_ENG
    p.pop("interval", None)
    p["targets"] = [tgt(ES_ENG, "A", CAND + " AND atscale.score:<$compression_threshold", [{"type": "count"}],
                        [terms(10, "atscale.dataset"), date_hist(11)], alias="{{term atscale.dataset}}")]


def es_candidates_per_query(p):
    p["datasource"] = ES_ENG
    p["targets"] = [raw(ES_ENG, "A", CAND)]
    fields = ["@timestamp"] + ["atscale." + f for f in g.CAND_FIELDS if f != "Time"]
    p["transformations"] = [{"id": "filterFieldsByName", "options": {"include": {"names": fields}}}, strip_prefix(),
                            {"id": "organize", "options": {"indexByName": {f: i for i, f in enumerate(["@timestamp"] + g.CAND_FIELDS[1:])},
                                                           "renameByName": {"@timestamp": "Time", "query_id": "Query ID", "dataset": "Fact dataset",
                                                                            "grain": "Aggregate grain", "score": "Compression score",
                                                                            "candidate": "Candidate ID"}}},
                            {"id": "sortBy", "options": {"sort": [{"field": "Compression score"}]}}]


def es_dist(p):
    p["datasource"] = {"type": "datasource", "uid": "-- Mixed --"}
    p["targets"] = [raw(ES_EXP, "A", "atscale.event:atscale_query AND " + EXP_FILTER + " AND atscale.optimization:none AND "
                        "atscale.status:successful AND atscale.duration_ms:[$min_runtime_ms TO $max_runtime_ms]", 5000),
                    raw(ES_ENG, "B", CAND, 5000)]
    # same matrix as generate.py, after the fields lost their atscale. prefix
    p["transformations"] = [{"id": "filterFieldsByName", "options": {"include": {
        "names": ["atscale.query_id", "atscale.runtime_bucket", "atscale.score_bucket"]}}}, strip_prefix()] + g.DIST_TRANSFORMS[1:]


def es_suggestions(p):
    p["datasource"] = {"type": "datasource", "uid": "-- Mixed --"}
    p["targets"] = [raw(ES_EXP, "A", "atscale.event:atscale_uda_suggestion AND " + EXP_FILTER +
                        " AND atscale.duration_ms:[$min_runtime_ms TO $max_runtime_ms]"),
                    raw(ES_ENG, "B", CAND + " AND atscale.score:<$compression_threshold")]
    # the candidate frame (B) keeps only join key, grain and score, so the join has no duplicate query_id/dataset
    keep_a = ["atscale.join_key"] + ["atscale." + f for f in g.SUG_FIELDS if f not in ("grain", "score")]
    keep_b = ["atscale.join_key", "atscale.grain", "atscale.score"]
    p["transformations"] = [{"id": "filterFieldsByName", "filter": {"id": "byRefId", "options": "A"},
                             "options": {"include": {"names": keep_a}}},
                            {"id": "filterFieldsByName", "filter": {"id": "byRefId", "options": "B"},
                             "options": {"include": {"names": keep_b}}},
                            {"id": "joinByField", "options": {"byField": "atscale.join_key", "mode": "inner"}},
                            strip_prefix()] + [t for t in p["transformations"] if t["id"] not in ("extractFields", "joinByField")]


def es_deepest(p):
    p["datasource"] = ES_ENG
    p.pop("interval", None)
    p["targets"] = [tgt(ES_ENG, "A", QUEUE, [{"type": "max", "field": "atscale.queue_position"}],
                        [terms(10, "atscale.connection"), date_hist(11)], alias="{{term atscale.connection}}")]


def es_queued_by_requester(p):
    p["datasource"] = ES_ENG
    p.pop("interval", None)
    p["targets"] = [tgt(ES_ENG, "A", QUEUE + " AND atscale.queue_position:>1", [{"type": "count"}],
                        [terms(10, "atscale.requester"), date_hist(11)], alias="{{term atscale.requester}}")]


def es_queue_table(p):
    p["datasource"] = ES_ENG
    by = [terms(10, "atscale.connection"), terms(11, "atscale.requester")]
    p["targets"] = [tgt(ES_ENG, "A", QUEUE, [{"type": "count"}, {"type": "max", "field": "atscale.queue_position"}], by),
                    tgt(ES_ENG, "B", QUEUE + " AND atscale.queue_position:>1", [{"type": "cardinality", "field": "atscale.request_id"}], by)]
    p["transformations"] = [{"id": "merge", "options": {}}, {"id": "organize", "options": {
        "renameByName": {"atscale.connection": "Connection", "atscale.requester": "Requester", "Count": "Requests",
                         "Unique Count": "Queued behind others", "Max": "Deepest place"},
        "indexByName": {"atscale.connection": 0, "atscale.requester": 1, "Count": 2, "Unique Count": 3,
                        "Max": 4}}}]


REPLACE = {
    "Slowest queries": es_slowest,
    "Query text for $query_id": es_query_text,
    "Warehouse connection queue for $query_id": es_queue_for_query,
    "Aggregate candidates by dataset and grain": es_candidates_by_grain,
    "Candidates below the compression threshold": es_candidates_below,
    "Aggregate candidates per query": es_candidates_per_query,
    "Unoptimized queries by runtime and compression score": es_dist,
    "Unoptimized queries by runtime, stacked by compression score": es_dist,
    "Aggregate suggestions for slow queries": es_suggestions,
    "Deepest place in the warehouse connection queue": es_deepest,
    "Requests that queued behind others, by requester": es_queued_by_requester,
    "Warehouse connection queue by connection and requester": es_queue_table,
}
QUERY_TEXT_LINK = ("/d/atscale-query-lifecycle-es?${__url_time_range}&${datasource:queryparam}&${es_engine:queryparam}"
                   "&${es_exporter:queryparam}&${engine_filter:queryparam}&${env:queryparam}&${query_type:queryparam}"
                   "&var-query_id=${__value.raw}")


def to_loki_free(text):
    return (text.replace("exporter's query log in Loki", "exporter's query log in Elasticsearch")
                .replace("AtScale engine log in Loki", "AtScale engine log in Elasticsearch")
                .replace("in Loki", "in Elasticsearch")
                .replace("(Engine log selector at the top)", "(Engine log filter at the top)")
                .replace("Needs Loki and the log selector at the top of the dashboard; the other panels work without it.",
                         "The other panels work without Elasticsearch."))


def build():
    dash = copy.deepcopy(g.dash)
    dash["uid"] = "atscale-query-lifecycle-es"
    dash["title"] = "AtScale query lifecycle (Elasticsearch logs)"
    dash["description"] = to_loki_free(dash["description"]) + " Logs from Elasticsearch."
    dash["tags"] = dash["tags"] + ["elasticsearch"]
    for p in dash["panels"]:
        if p["type"] == "row":
            p["title"] = to_loki_free(p["title"])
            continue
        fix = REPLACE.get(p["title"])
        if p.get("datasource", {}).get("type") == "loki":
            if not fix:
                raise SystemExit("no Elasticsearch replacement for Loki panel: " + p["title"])
            fix(p)
            for t in p["targets"]:
                t.pop("expr", None)
        if "description" in p:
            p["description"] = to_loki_free(p["description"]) + (ES_NOTE if fix else "")
    drop = {"loki", "engine_log_selector", "log_selector"}
    tl = []
    for v in dash["templating"]["list"]:
        if v["name"] in drop:
            continue
        # Lucene formatting (${var:lucene}) needs the real values, not the PromQL regex ".*"
        v.pop("allValue", None)
        tl.append(v)
        if v["name"] == "datasource":
            tl += [{"name": "es_engine", "label": "Elasticsearch engine log", "type": "datasource", "query": "elasticsearch",
                    "regex": "/engine/i", "refresh": 1, "hide": 0},
                   {"name": "es_exporter", "label": "Elasticsearch exporter", "type": "datasource", "query": "elasticsearch",
                    "regex": "/exporter/i", "refresh": 1, "hide": 0},
                   {"name": "engine_filter", "label": "Engine log filter (Lucene)", "type": "textbox", "query": "*",
                    "current": {"text": "*", "value": "*"}, "hide": 0}]
    dash["templating"]["list"] = tl
    return dash


if __name__ == "__main__":
    dash = build()
    for path in OUTPUTS:
        path.write_text(json.dumps(dash, indent=2) + "\n")
        print("wrote", path.relative_to(ROOT))
