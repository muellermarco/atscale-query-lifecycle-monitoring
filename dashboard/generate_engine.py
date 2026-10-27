"""Generate the Grafana dashboard that reads only AtScale engine telemetry (no exporter, no AtScale API).

Sample code, provided as is under the MIT License. Not an AtScale product. See DISCLAIMER.md.

Purpose: a second dashboard next to atscale-query-lifecycle.json that uses only the engine's own
telemetry: OpenTelemetry traces in Tempo (TraceQL metrics and search) and the engine log in Loki.
It shows what you get without the exporter, and a text panel lists what is missing.
Inputs: the LogQL definitions in generate.py. Outputs: dashboard/atscale-query-lifecycle-engine.json
and the copy the Helm chart ships.
Run: python3 dashboard/generate_engine.py
"""
import json
from pathlib import Path

from generate import ENG, QUEUE, QUEUE_HELP, SCORE_HELP

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = [ROOT / "dashboard" / "atscale-query-lifecycle-engine.json",
           ROOT / "deploy" / "helm" / "atscale-query-lifecycle" / "files" / "atscale-query-lifecycle-engine.json"]

TEMPO = {"type": "tempo", "uid": "${tempo}"}
LOKI = {"type": "loki", "uid": "${loki}"}
ENGINE_LOG = '{container="engine", namespace="$namespace"}'
NS = 'resource.k8s.namespace.name="$namespace"'
USER = '{%s && span.queryType="User"}' % NS
TRACE_HELP = ("From the AtScale engine's OpenTelemetry traces in Tempo (one trace per query, root span named after the query ID). "
              "Needs the AtScale telemetry collector to export traces to Tempo with the resource attribute k8s.namespace.name, "
              "and the Tempo metrics generator (local-blocks) for TraceQL metrics.")

pid = [0]


def nid():
    pid[0] += 1
    return pid[0]


def row(title, y):
    return {"type": "row", "title": title, "id": nid(), "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


def traceql(query, ref="A", legend=""):
    """A TraceQL metrics target (range query)."""
    t = {"datasource": TEMPO, "refId": ref, "queryType": "traceql", "query": query, "metricsQueryType": "range"}
    if legend:
        t["legendFormat"] = legend
    return t


def stat(title, query, unit, x, y, desc, calc="sum"):
    return {"type": "stat", "title": title, "id": nid(), "datasource": TEMPO, "description": desc + " " + TRACE_HELP,
            "gridPos": {"h": 4, "w": 6, "x": x, "y": y}, "targets": [traceql(query)],
            "options": {"reduceOptions": {"calcs": [calc], "values": False, "fields": ""},
                        "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "center"},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 0 if unit == "short" else 2, "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}},
                            "overrides": []}}


def series(title, targets, unit, x, y, desc, w=12, h=8, stack=False):
    custom = {"drawStyle": "line", "fillOpacity": 10, "showPoints": "never"}
    if stack:
        custom.update(drawStyle="bars", fillOpacity=80, stacking={"mode": "normal"})
    return {"type": "timeseries", "title": title, "id": nid(), "datasource": TEMPO, "description": desc + " " + TRACE_HELP,
            "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}}}


panels = []
y = 0
panels.append(row("Overview from engine traces (Tempo)", y)); y += 1
panels += [
    stat("User queries", USER + " | count_over_time()", "short", 0, y, "User queries that started in the selected range."),
    stat("p95 duration", USER + " | quantile_over_time(duration, .95)", "s", 6, y,
         "95th percentile of the end-to-end server time per user query (root span duration), highest interval in the range.", calc="max"),
    stat("Failed user queries", '{%s && span.queryType="User" && status=error} | count_over_time()' % NS, "short", 12, y,
         "User queries whose root span has status error. The error message is in the QueryFailedEvent of the trace."),
    stat("Warehouse queries", USER + " >> {span.subqueryId != nil} | count_over_time()", "short", 18, y,
         "Warehouse queries (subqueries) that user queries sent."),
]
y += 4
panels += [
    series("Queries per second by type", [traceql("{%s && span.queryType != nil} | rate() by (span.queryType)" % NS)], "reqps", 0, y,
           "User queries and AtScale's own system queries (aggregate builds, statistics)."),
    series("User query duration", [traceql(USER + " | quantile_over_time(duration, .5, .95)")], "s", 12, y,
           "Median and 95th percentile of the end-to-end server time per user query."),
]
y += 8
panels += [
    series("p95 by phase", [
        traceql(USER + " | quantile_over_time(duration, .95)", "A", "Total"),
        traceql(USER + ' > {event:name="QueryPlanningStartedEvent"} | quantile_over_time(duration, .95)', "B", "Planning"),
        traceql(USER + " >> {span.subqueryId != nil} | quantile_over_time(duration, .95)", "C", "Warehouse query"),
    ], "s", 0, y,
        "95th percentile per phase: Total (root span), Planning (span with QueryPlanningStartedEvent) and one warehouse query "
        "(subquery span). Traces do not split the outbound phase into connection wait and warehouse time."),
    series("p95 duration by model ID", [traceql(USER + " | quantile_over_time(duration, .95) by (span.modelId)")], "s", 12, y,
           "Traces carry the model ID, not the model name. Look the ID up in AtScale Design Center or the catalog API."),
]
y += 8
panels += [
    series("User queries by client language", [traceql(USER + " | rate() by (event.language)")], "reqps", 0, y,
           "From the QueryReceivedEvent: analysis = MDX or DAX over XMLA (Excel, Power BI), pgsql = the SQL interface.", stack=True),
    series("Warehouse queries by aggregate cache use", [
        traceql(USER + " >> {span.subqueryId != nil && event.usedAggregateCache != nil} | rate() by (event.usedAggregateCache)")],
        "reqps", 12, y, "From the SubquerySucceededEvent of each warehouse query: True means AtScale answered it from the aggregate cache.",
        stack=True),
]
y += 8

panels.append(row("Single queries (Tempo search and engine log)", y)); y += 1
QUERY_LINK = "/d/atscale-query-lifecycle-engine?${__url_time_range}&${tempo:queryparam}&${loki:queryparam}&${namespace:queryparam}&${min_duration:queryparam}&var-query_id=${__value.raw}"
panels.append({
    "type": "table", "title": "Slowest user queries", "id": nid(), "datasource": TEMPO,
    "description": "User queries longer than the Min duration field, from Tempo search. Click a Trace ID to open the waterfall, "
                   "or a Name (the query ID) to show its query text and connection queue below. " + TRACE_HELP,
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": y},
    "targets": [{"datasource": TEMPO, "refId": "A", "queryType": "traceql", "tableType": "traces", "limit": 100,
                 "query": '{%s && span.queryType="User" && duration > $min_duration} | select(span.modelId, span.userId)' % NS}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True}},
                    "overrides": [{"matcher": {"id": "byName", "options": "Name"},
                                   "properties": [{"id": "links", "value": [{"title": "Show query text and queue", "url": QUERY_LINK}]}]}]},
    "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Duration", "desc": True}]}})
y += 10
panels.append({
    "type": "logs", "title": "Query text for $query_id", "id": nid(), "datasource": LOKI,
    "description": "Inbound MDX, DAX or SQL and the outbound warehouse SQL from the engine log in Loki. Needs a log collector that "
                   "joins multi-line engine log entries. The engine log contains user names and filter values.",
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "maxLines": 200,
                 "expr": ENGINE_LOG + r' |~ "queryId=${query_id}[,}]" |~ "Received (MDX|DAX|PGSQL) query|Executing SQL Query"'}],
    "options": {"showTime": True, "wrapLogMessage": True, "prettifyLogMessage": False, "enableLogDetails": True,
                "sortOrder": "Ascending", "dedupStrategy": "none"}})
y += 12
QUEUE_E = QUEUE.replace("$engine_log_selector", ENGINE_LOG)
QQ = r'%s | query_id="${query_id}"' % QUEUE_E
panels.append({
    "type": "table", "title": "Warehouse connection queue for $query_id", "id": nid(), "datasource": LOKI,
    "description": "Warehouse queries of the selected query by connection, and the deepest place in the connection queue. " + QUEUE_HELP,
    "gridPos": {"h": 5, "w": 24, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "instant", "expr": "sum by (connection) (count_over_time(%s [$__range]))" % QQ},
                {"datasource": LOKI, "refId": "B", "queryType": "instant", "expr": "sum by (connection) (count_over_time(%s | position > 1 [$__range]))" % QQ},
                {"datasource": LOKI, "refId": "C", "queryType": "instant", "expr": "max by (connection) (max_over_time(%s | unwrap position [$__range]))" % QQ}],
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                       "renameByName": {"connection": "Connection", "Value #A": "Warehouse queries",
                                                        "Value #B": "Queued behind others", "Value #C": "Deepest place"},
                                       "indexByName": {"connection": 0, "Value #A": 1, "Value #B": 2, "Value #C": 3}}}],
    "fieldConfig": {"defaults": {"unit": "short", "decimals": 0}, "overrides": []},
    "options": {"showHeader": True, "cellHeight": "sm"}})
y += 5

panels.append(row("Warehouse connection queue and aggregate candidates (engine log)", y)); y += 1
panels += [
    {"type": "timeseries", "title": "Requests that queued behind others, by requester", "id": nid(), "datasource": LOKI,
     "description": "Warehouse connection requests with a queue place above 1, by who asked. " + QUEUE_HELP,
     "gridPos": {"h": 8, "w": 12, "x": 0, "y": y}, "interval": "5m",
     "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "legendFormat": "{{requester}}",
                  "expr": "sum by (requester) (count_over_time(%s | position > 1 [$__interval]))" % QUEUE_E}],
     "fieldConfig": {"defaults": {"unit": "short", "decimals": 0, "custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}}},
                     "overrides": []},
     "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}}},
    {"type": "table", "title": "Aggregate candidates by dataset and grain", "id": nid(), "datasource": LOKI,
     "description": SCORE_HELP,
     "gridPos": {"h": 8, "w": 12, "x": 12, "y": y},
     "targets": [{"datasource": LOKI, "refId": "A", "queryType": "instant",
                  "expr": "avg by (dataset, grain) (avg_over_time(%s | unwrap score [$__range]))" % ENG.replace("$engine_log_selector", ENGINE_LOG)
                          .replace("| score >= $min_score | score <= $max_score", "")}],
     "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                        "renameByName": {"dataset": "Fact dataset", "grain": "Aggregate grain", "Value": "Compression score"}}},
                         {"id": "sortBy", "options": {"sort": [{"field": "Compression score"}]}}],
     "fieldConfig": {"defaults": {"decimals": 2}, "overrides": []},
     "options": {"showHeader": True, "cellHeight": "sm"}},
]
y += 8

panels.append(row("Compared with the exporter dashboard", y)); y += 1
panels.append({
    "type": "text", "title": "What engine telemetry alone does not give you", "id": nid(),
    "gridPos": {"h": 9, "w": 24, "x": 0, "y": y},
    "options": {"mode": "markdown", "content": "\n".join([
        "This dashboard uses no exporter and no AtScale API. Compared with **AtScale query lifecycle** (exporter + query history API):",
        "",
        "| Topic | Engine telemetry (this dashboard) | Exporter dashboard |",
        "|---|---|---|",
        "| Catalog and model | IDs only | Names |",
        "| Phases | Total, planning, each warehouse query | Inbound, planning, outbound wait, warehouse, result processing |",
        "| Why a query missed aggregates | Aggregate cache use per warehouse query only | Miss reason per query, aggregate suggestions |",
        "| Query text | Inbound in the trace, outbound in the engine log | Not in the API, engine log only |",
        "| Waterfall of one query | Yes (Tempo) | No |",
        "| History | Tempo and Loki retention (72 hours here) | Prometheus retention |",
        "| Setup on the AtScale side | Collector config override, AtScale upgrade restarts all pods | API token only |",
        "",
        "Both depend on undocumented formats: span and event names here, the engine log text in both dashboards.",
    ])}})
y += 9

dash = {
    "uid": "atscale-query-lifecycle-engine", "title": "AtScale queries from engine telemetry",
    "description": "AtScale query performance from the engine's own OpenTelemetry traces (Tempo) and engine log (Loki), without the "
                   "exporter or the AtScale API. Sample code, provided as is, not supported by AtScale.",
    "tags": ["atscale", "queries", "performance", "tempo"], "timezone": "browser", "editable": True,
    "schemaVersion": 39, "version": 1, "refresh": "1m", "graphTooltip": 1,
    "time": {"from": "now-3h", "to": "now"},
    "templating": {"list": [
        {"name": "tempo", "label": "Tempo", "type": "datasource", "query": "tempo", "refresh": 1, "hide": 0},
        {"name": "loki", "label": "Loki", "type": "datasource", "query": "loki", "refresh": 1, "hide": 0},
        {"name": "namespace", "label": "AtScale namespace", "type": "query", "datasource": LOKI,
         "query": 'label_values({container="engine"}, namespace)', "definition": 'label_values({container="engine"}, namespace)',
         "refresh": 2, "multi": False, "includeAll": False, "sort": 1},
        {"name": "min_duration", "label": "Min duration", "type": "textbox", "query": "1s",
         "current": {"text": "1s", "value": "1s"}, "hide": 0},
        {"name": "query_id", "label": "Query ID", "type": "textbox", "query": "",
         "current": {"text": "", "value": ""}, "hide": 0},
    ]},
    "annotations": {"list": []},
    "links": [{"title": "AtScale query lifecycle (exporter)", "type": "link", "url": "/d/atscale-query-lifecycle", "icon": "dashboard"}],
    "panels": panels,
}

if __name__ == "__main__":
    for path in OUTPUTS:
        path.write_text(json.dumps(dash, indent=2) + "\n")
        print("wrote", path.relative_to(ROOT))
