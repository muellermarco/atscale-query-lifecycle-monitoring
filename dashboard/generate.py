"""Generate the Grafana dashboard for the AtScale query lifecycle exporter.

Sample code, provided as is under the MIT License. Not an AtScale product. See DISCLAIMER.md.

Purpose: writes the dashboard JSON from one definition, so the panels and PromQL stay consistent.
Inputs: none. Outputs: dashboard/atscale-query-lifecycle.json and the copy the Helm chart ships.
Run: python3 dashboard/generate.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = [ROOT / "dashboard" / "atscale-query-lifecycle.json",
           ROOT / "deploy" / "helm" / "atscale-query-lifecycle" / "files" / "atscale-query-lifecycle.json"]

DS = {"type": "prometheus", "uid": "${datasource}"}
F = 'env=~"$env",query_type=~"$query_type",catalog=~"$catalog",model=~"$model",optimization=~"$optimization",status=~"$status"'
Q = "atscale_query_duration_seconds"
P = "atscale_query_phase_duration_seconds"
PH = 'phase=~"inbound|planning|outbound_wait|warehouse|result_processing"'
PHASE_COLORS = {"inbound": "#8AB8FF", "planning": "#FFB357", "outbound_wait": "#F2495C",
                "warehouse": "#5794F2", "result_processing": "#73BF69"}
PHASE_NAMES = {"inbound": "Inbound Query", "planning": "Planning", "outbound_wait": "Outbound wait",
               "warehouse": "Outbound warehouse", "result_processing": "Result Processing", "outbound": "Outbound (total)"}

pid = [0]
def nid():
    pid[0] += 1; return pid[0]

def tgt(expr, legend="", ref="A", instant=False, fmt="time_series"):
    t = {"datasource": DS, "expr": expr, "refId": ref, "legendFormat": legend, "format": fmt}
    if instant: t.update(instant=True, range=False)
    return t

def phase_overrides():
    out = []
    for k, c in PHASE_COLORS.items():
        out.append({"matcher": {"id": "byName", "options": PHASE_NAMES[k]},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]})
    return out

def phase_legend(expr):
    # map phase label to readable names via label_replace chain
    for k, v in PHASE_NAMES.items():
        expr = 'label_replace(%s, "phase", "%s", "phase", "%s")' % (expr, v, k)
    return expr

def row(title, y, desc=""):
    return {"type": "row", "title": title, "id": nid(), "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}

def stat(title, expr, unit, x, y, w=4, desc="", decimals=None, thresholds=None):
    p = {"type": "stat", "title": title, "id": nid(), "datasource": DS, "description": desc,
         "gridPos": {"h": 4, "w": w, "x": x, "y": y},
         "targets": [tgt(expr, instant=True)],
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
                     "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "center"},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                         "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "text", "value": None}]}},
                         "overrides": []}}
    if decimals is not None: p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p

panels = []
y = 0
panels.append(row("Overview for the selected range", y)); y += 1
panels += [
    stat("Queries", 'sum(increase(%s_count{%s}[$__range]))' % (Q, F), "short", 0, y, decimals=0,
         desc="Queries that finished in the selected range (from the AtScale query history)."),
    stat("Avg duration", 'sum(increase(%s_sum{%s}[$__range])) / sum(increase(%s_count{%s}[$__range]))' % (Q, F, Q, F), "s", 4, y,
         desc="End-to-end server time per query: Inbound Query + Planning + Outbound + Result Processing."),
    stat("p50", 'histogram_quantile(0.50, sum by (le) (increase(%s_bucket{%s}[$__range])))' % (Q, F), "s", 8, y,
         desc="Median, estimated from histogram buckets."),
    stat("p95", 'histogram_quantile(0.95, sum by (le) (increase(%s_bucket{%s}[$__range])))' % (Q, F), "s", 12, y,
         desc="95th percentile, estimated from histogram buckets."),
    stat("Aggs or cache", 'sum(increase(%s_count{%s,optimization!="none"}[$__range])) / sum(increase(%s_count{%s}[$__range]))' % (Q, F, Q, F),
         "percentunit", 16, y, desc="Share of queries AtScale answered from an aggregate table or the query cache."),
    stat("Exporter up", 'min(atscale_query_exporter_up{env=~"$env"})', "short", 20, y,
         desc="1 = the last poll of every selected AtScale instance succeeded.",
         thresholds={"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]}),
]
y += 4

panels.append(row("Where the time goes", y)); y += 1
avg_phase = phase_legend('sum by (phase) (increase(%s_sum{%s,%s}[$__range])) / sum by (phase) (increase(%s_count{%s,%s}[$__range]))' % (P, F, PH, P, F, PH))
panels.append({
    "type": "bargauge", "title": "Average time per phase", "id": nid(), "datasource": DS,
    "description": "Average per query. Outbound is split into the time spent waiting before the warehouse call (connection pool, queueing, SQL generation) and the time the warehouse subqueries ran (parallel subqueries counted once).",
    "gridPos": {"h": 9, "w": 12, "x": 0, "y": y},
    "targets": [tgt(avg_phase, "{{phase}}", instant=True)],
    "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""}, "namePlacement": "left", "valueMode": "color", "minVizWidth": 0},
    "fieldConfig": {"defaults": {"unit": "s", "min": 0, "decimals": 1, "color": {"mode": "fixed", "fixedColor": "#5794F2"}},
                    "overrides": phase_overrides()}})
share = phase_legend('sum by (phase) (increase(%s_sum{%s,%s}[$__range]))' % (P, F, PH))
panels.append({
    "type": "piechart", "title": "Share of total query time", "id": nid(), "datasource": DS,
    "description": "Which phase consumes most of the summed server time in the range.",
    "gridPos": {"h": 9, "w": 12, "x": 12, "y": y},
    "targets": [tgt(share, "{{phase}}", instant=True)],
    "options": {"pieType": "donut", "legend": {"displayMode": "table", "placement": "bottom", "values": ["percent"]},
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
                "tooltip": {"mode": "single"}},
    "fieldConfig": {"defaults": {"unit": "s"}, "overrides": phase_overrides()}})
pq = []
for i, (q, label) in enumerate([("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99")]):
    pq.append(tgt(phase_legend('histogram_quantile(%s, sum by (le, phase) (increase(%s_bucket{%s,%s}[$__range])))' % (q, P, F, PH)),
                  "{{phase}}", ref=chr(66 + i), instant=True, fmt="table"))
pq.insert(0, tgt(avg_phase, "{{phase}}", ref="A", instant=True, fmt="table"))
panels.append({
    "type": "table", "title": "Phase statistics", "id": nid(), "datasource": DS,
    "description": "Average and percentiles per phase. Percentiles are estimated from histogram buckets.",
    "gridPos": {"h": 7, "w": 24, "x": 0, "y": y + 9}, "targets": pq,
    "transformations": [
        {"id": "joinByField", "options": {"byField": "phase", "mode": "outer"}},
        {"id": "organize", "options": {"excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True},
                                       "renameByName": {"phase": "Phase", "Value #A": "Average", "Value #B": "p50", "Value #C": "p95", "Value #D": "p99"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Average", "desc": True}]}}],
    "fieldConfig": {"defaults": {"unit": "s", "decimals": 1}, "overrides": []},
    "options": {"showHeader": True, "cellHeight": "sm"}})
y += 16
ts_phase = phase_legend('sum by (phase) (increase(%s_sum{%s,%s}[$__interval])) / sum by (phase) (increase(%s_count{%s,%s}[$__interval]))' % (P, F, PH, P, F, PH))
panels.append({
    "type": "timeseries", "title": "Average time per phase over time (stacked)", "id": nid(), "datasource": DS,
    "description": "Stack height = average query duration in that interval. Gaps mean no queries.",
    "gridPos": {"h": 9, "w": 24, "x": 0, "y": y}, "interval": "5m",
    "targets": [tgt(ts_phase, "{{phase}}")],
    "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["mean", "max"]}, "tooltip": {"mode": "multi", "sort": "desc"}},
    "fieldConfig": {"defaults": {"unit": "s", "custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}, "lineWidth": 1}},
                    "overrides": phase_overrides()}})
y += 9

panels.append(row("Which catalogs and models drive it", y)); y += 1
def by_cm(expr, ref):
    return tgt(expr, ref=ref, instant=True, fmt="table")
cm = "catalog, model"
def avg_ph(ph):
    return 'sum by (%s) (increase(%s_sum{%s,phase="%s"}[$__range])) / sum by (%s) (increase(%s_count{%s,phase="%s"}[$__range]))' % (cm, P, F, ph, cm, P, F, ph)
targets = [
    by_cm('sum by (%s) (increase(%s_count{%s}[$__range]))' % (cm, Q, F), "A"),
    by_cm('sum by (%s) (increase(%s_sum{%s}[$__range])) / sum by (%s) (increase(%s_count{%s}[$__range]))' % (cm, Q, F, cm, Q, F), "B"),
    by_cm('histogram_quantile(0.95, sum by (%s, le) (increase(%s_bucket{%s}[$__range])))' % (cm, Q, F), "C"),
    by_cm(avg_ph("planning"), "D"),
    by_cm(avg_ph("outbound_wait"), "E"),
    by_cm(avg_ph("warehouse"), "F"),
    by_cm('sum by (%s) (increase(%s_count{%s,optimization!="none"}[$__range])) / sum by (%s) (increase(%s_count{%s}[$__range]))' % (cm, Q, F, cm, Q, F), "G"),
    by_cm('sum by (%s) (increase(atscale_query_subqueries_sum{%s}[$__range])) / sum by (%s) (increase(atscale_query_subqueries_count{%s}[$__range]))' % (cm, F, cm, F), "H"),
]
names = {"Value #A": "Queries", "Value #B": "Avg total", "Value #C": "p95 total", "Value #D": "Avg planning",
         "Value #E": "Avg outbound wait", "Value #F": "Avg warehouse", "Value #G": "Aggregate or cache hits", "Value #H": "Avg subqueries"}
panels.append({
    "type": "table", "title": "Catalogs and models, slowest first", "id": nid(), "datasource": DS,
    "description": "Per catalog and model. A high warehouse share with few aggregate hits points to missing aggregates; high planning points to model complexity; high outbound wait points to connection or queue limits.",
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": y}, "targets": targets,
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": dict(names, catalog="Catalog", model="Model"),
                                       "indexByName": {"catalog": 0, "model": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4, "Value #D": 5, "Value #E": 6, "Value #F": 7, "Value #G": 8, "Value #H": 9}}},
        {"id": "filterByValue", "options": {"type": "exclude", "match": "any", "filters": [{"fieldName": "Queries", "config": {"id": "lowerOrEqual", "options": {"value": 0}}}]}},
        {"id": "sortBy", "options": {"sort": [{"field": "Avg total", "desc": True}]}}],
    "fieldConfig": {"defaults": {"unit": "s", "decimals": 3, "custom": {"align": "auto"}},
                    "overrides": [
                        {"matcher": {"id": "byName", "options": "Queries"}, "properties": [{"id": "unit", "value": "short"}, {"id": "decimals", "value": 0}]},
                        {"matcher": {"id": "byName", "options": "Avg subqueries"}, "properties": [{"id": "unit", "value": "short"}, {"id": "decimals", "value": 1}]},
                        {"matcher": {"id": "byName", "options": "Aggregate or cache hits"}, "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 0},
                            {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}}, {"id": "min", "value": 0}, {"id": "max", "value": 1}]},
                        {"matcher": {"id": "byName", "options": "Avg total"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "gradient"}},
                            {"id": "color", "value": {"mode": "continuous-GrYlRd"}}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"}})
y += 10
fo = 'env=~"$env",query_type=~"$query_type",catalog=~"$catalog",model=~"$model",status=~"$status"'
panels.append({
    "type": "bargauge", "title": "Average time per phase by optimization", "id": nid(), "datasource": DS,
    "description": "AGGS = answered from an aggregate table, CACHE = query cache, none = raw tables. The warehouse bar shows what aggregates save.",
    "gridPos": {"h": 9, "w": 12, "x": 0, "y": y},
    "targets": [tgt(phase_legend('sum by (optimization, phase) (increase(%s_sum{%s,%s}[$__range])) / sum by (optimization, phase) (increase(%s_count{%s,%s}[$__range]))' % (P, fo, PH, P, fo, PH)),
                    "{{optimization}} · {{phase}}", instant=True)],
    "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "namePlacement": "left",
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""}},
    "fieldConfig": {"defaults": {"unit": "s", "min": 0, "decimals": 3, "color": {"mode": "fixed", "fixedColor": "#5794F2"}}, "overrides": []}})
panels.append({
    "type": "heatmap", "title": "Query duration distribution", "id": nid(), "datasource": DS,
    "description": "How many queries fell into each duration bucket per interval. Bands far above the main cluster are the outliers to inspect in the AtScale Queries page.",
    "gridPos": {"h": 9, "w": 12, "x": 12, "y": y}, "interval": "5m",
    "targets": [tgt('sum by (le) (increase(%s_bucket{%s}[$__interval]))' % (Q, F), "{{le}}", fmt="heatmap")],
    "options": {"calculate": False, "yAxis": {"unit": "s"}, "cellGap": 1, "color": {"mode": "scheme", "scheme": "Blues", "steps": 64},
                "tooltip": {"mode": "single", "yHistogram": True}, "legend": {"show": True}},
    "fieldConfig": {"defaults": {}, "overrides": []}})
y += 9

LOKI = {"type": "loki", "uid": "${loki}"}
LOG_FIELDS = ["start", "query_id", "catalog", "model", "query_type", "optimization", "status", "duration_ms",
              "inbound_ms", "planning_ms", "outbound_wait_ms", "warehouse_ms", "result_processing_ms", "subqueries"]
panels.append(row("Single queries (needs the exporter's query log in Loki)", y)); y += 1
panels.append({
    "type": "table", "title": "Slowest queries", "id": nid(), "datasource": LOKI,
    "description": "One row per query from the exporter's JSON query log, slowest first. Copy the query_id and search for it on the Queries page in AtScale Design Center. Needs Loki and the log selector at the top of the dashboard; the other panels work without it.",
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "maxLines": 1000,
                 "expr": '$log_selector |= "atscale_query" | json | event="atscale_query" | env=~"$env" | query_type=~"$query_type" | catalog=~"$catalog" | model=~"$model" | optimization=~"$optimization" | status=~"$status"'}],
    "transformations": [
        {"id": "extractFields", "options": {"source": "Line", "format": "json", "replace": True, "keepTime": False}},
        {"id": "filterFieldsByName", "options": {"include": {"names": LOG_FIELDS}}},
        {"id": "organize", "options": {"indexByName": {n: i for i, n in enumerate(LOG_FIELDS)},
                                       "renameByName": {"start": "Start (UTC)", "query_id": "Query ID", "catalog": "Catalog", "model": "Model",
                                                        "query_type": "Type", "optimization": "Optimization", "status": "Status",
                                                        "duration_ms": "Total", "inbound_ms": "Inbound Query", "planning_ms": "Planning",
                                                        "outbound_wait_ms": "Outbound wait", "warehouse_ms": "Outbound warehouse",
                                                        "result_processing_ms": "Result Processing", "subqueries": "Subqueries"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Total", "desc": True}]}}],
    "fieldConfig": {"defaults": {"unit": "ms", "decimals": 1, "custom": {"align": "auto", "filterable": True}},
                    "overrides": [
                        {"matcher": {"id": "byName", "options": "Subqueries"}, "properties": [{"id": "unit", "value": "short"}, {"id": "decimals", "value": 0}]},
                        {"matcher": {"id": "byName", "options": "Total"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "gradient"}},
                            {"id": "color", "value": {"mode": "continuous-GrYlRd"}}]},
                        {"matcher": {"id": "byName", "options": "Query ID"}, "properties": [{"id": "custom.minWidth", "value": 300}]}]},
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}})
y += 12

panels.append(row("Engine JDBC pools (engine OpenTelemetry metrics)", y)); y += 1
panels.append({
    "type": "timeseries", "title": "Average JDBC connection borrow time by pool", "id": nid(), "datasource": DS,
    "description": "From the engine's Kamon/OpenTelemetry metrics on :9095. Most JDBC traffic is the AtScale system database, so use this only to check whether a pool wait explains a high outbound wait.",
    "gridPos": {"h": 8, "w": 12, "x": 0, "y": y},
    "targets": [tgt('sum by (env, jdbc_pool_name) (rate(jdbc_pool_borrow_time_seconds_sum{env=~"$env"}[$__rate_interval])) / sum by (env, jdbc_pool_name) (rate(jdbc_pool_borrow_time_seconds_count{env=~"$env"}[$__rate_interval]))', "{{env}} {{jdbc_pool_name}}")],
    "fieldConfig": {"defaults": {"unit": "s", "custom": {"fillOpacity": 10}}, "overrides": []},
    "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
panels.append({
    "type": "timeseries", "title": "JDBC borrow timeouts and statements in flight", "id": nid(), "datasource": DS,
    "description": "Borrow timeouts > 0 mean queries waited for a free connection and gave up.",
    "gridPos": {"h": 8, "w": 12, "x": 12, "y": y},
    "targets": [tgt('sum by (env) (increase(jdbc_pool_borrow_timeouts_total{env=~"$env"}[$__rate_interval]))', "{{env}} borrow timeouts", "A"),
                tgt('sum by (env) (rate(jdbc_statements_in_flight_sum{env=~"$env"}[$__rate_interval])) / sum by (env) (rate(jdbc_statements_in_flight_count{env=~"$env"}[$__rate_interval]))', "{{env}} avg statements in flight", "B")],
    "fieldConfig": {"defaults": {"unit": "short", "custom": {"fillOpacity": 10}}, "overrides": []},
    "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})

def var(name, label, query, multi=True, include_all=True, current=None):
    v = {"name": name, "label": label, "type": "query", "datasource": DS,
         "query": {"query": query, "refId": name}, "definition": query, "refresh": 2,
         "multi": multi, "includeAll": include_all, "sort": 1}
    if include_all: v["allValue"] = ".*"
    if current: v["current"] = current
    return v

base = "%s_count" % Q
dash = {
    "uid": "atscale-query-lifecycle", "title": "AtScale query lifecycle",
    "description": "Inbound Query, Planning, Outbound and Result Processing per AtScale query, from the AtScale query history. Sample code, provided as is, not supported by AtScale.",
    "tags": ["atscale", "queries", "performance"], "timezone": "browser", "editable": True,
    "schemaVersion": 39, "version": 1, "refresh": "1m", "graphTooltip": 1,
    "time": {"from": "now-24h", "to": "now"},
    "templating": {"list": [
        {"name": "datasource", "label": "Prometheus", "type": "datasource", "query": "prometheus", "refresh": 1, "hide": 0},
        {"name": "loki", "label": "Loki", "type": "datasource", "query": "loki", "refresh": 1, "hide": 0},
        {"name": "log_selector", "label": "Log selector", "type": "textbox", "query": '{container="exporter"}',
         "current": {"text": '{container="exporter"}', "value": '{container="exporter"}'}, "hide": 0},
        var("env", "Instance", 'label_values(%s, env)' % base, multi=False, include_all=False),
        var("query_type", "Query type", 'label_values(%s{env=~"$env"}, query_type)' % base, current={"text": ["User"], "value": ["User"], "selected": True}),
        var("catalog", "Catalog", 'label_values(%s{env=~"$env"}, catalog)' % base),
        var("model", "Model", 'label_values(%s{env=~"$env",catalog=~"$catalog"}, model)' % base),
        var("optimization", "Optimization", 'label_values(%s{env=~"$env"}, optimization)' % base),
        var("status", "Status", 'label_values(%s{env=~"$env"}, status)' % base),
    ]},
    "annotations": {"list": []},
    "links": [],
    "panels": panels,
}
for path in OUTPUTS:
    path.write_text(json.dumps(dash, indent=2) + "\n")
    print("wrote", path.relative_to(ROOT))
