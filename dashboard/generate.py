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

def stat(title, expr, unit, x, y, w=4, desc="", decimals=None, thresholds=None, time_from=None):
    p = {"type": "stat", "title": title, "id": nid(), "datasource": DS, "description": desc,
         "gridPos": {"h": 4, "w": w, "x": x, "y": y},
         "targets": [tgt(expr, instant=True)],
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
                     "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "center"},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                         "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "text", "value": None}]}},
                         "overrides": []}}
    if decimals is not None: p["fieldConfig"]["defaults"]["decimals"] = decimals
    if time_from: p["timeFrom"] = time_from
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
    stat("Aggs or cache", 'sum(increase(%s_count{%s,optimization!="none"}[$__range])) / sum(increase(%s_count{%s,miss_reason!="no_warehouse"}[$__range]))' % (Q, F, Q, F),
         "percentunit", 16, y, desc="Share of queries AtScale answered from an aggregate table or the query cache. Queries that never reached the warehouse are left out."),
    stat("Exporter up", 'min(atscale_query_exporter_up{env=~"$env"})', "short", 20, y,
         desc="1 = the last poll of every selected AtScale instance succeeded. Always shows the last 5 minutes, whatever the dashboard time range.",
         thresholds={"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]},
         time_from="5m"),   # a status of now, not of the selected range
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

REASON_NAMES = {"aggregate_created": "Aggregate created by this query", "no_aggregate_created": "No aggregate created: investigate",
                "no_warehouse": "No warehouse query", "system": "AtScale system query", "failed": "Failed", "unclassified": "Unclassified"}
REASON_COLORS = {"aggregate_created": "#73BF69", "no_aggregate_created": "#F2495C", "no_warehouse": "#8AB8FF",
                 "system": "#CA95E5", "failed": "#FF9830", "unclassified": "#B7B7B7"}
REASON_HELP = ("Why queries ran without an aggregate or the cache. Aggregate created by this query: the first run of a new combination "
               "of attributes and measures; AtScale created an aggregate while it ran, so the next run can use it. "
               "No aggregate created: investigate, for example an exact distinct count that no aggregate holds, or a catalog "
               "deployed while the query ran. No warehouse query: answered without a warehouse query (metadata). "
               "AtScale system query: AtScale's own queries, for example aggregate builds and statistics. "
               "Unclassified: the exporter could not read the aggregates API.")

def reason_legend(expr):
    for k, v in REASON_NAMES.items():
        expr = 'label_replace(%s, "miss_reason", "%s", "miss_reason", "%s")' % (expr, v, k)
    return expr

def reason_overrides():
    return [{"matcher": {"id": "byName", "options": REASON_NAMES[k]},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]} for k, c in REASON_COLORS.items()]

MISS = 'miss_reason=~".+",miss_reason!="optimized"'
panels.append(row("Why queries missed aggregates and cache", y)); y += 1
panels.append({
    "type": "bargauge", "title": "Queries without aggregate or cache, by reason", "id": nid(), "datasource": DS,
    "description": REASON_HELP,
    "gridPos": {"h": 9, "w": 9, "x": 0, "y": y},
    "targets": [tgt(reason_legend('sum by (miss_reason) (increase(%s_count{%s,%s}[$__range]))' % (Q, F, MISS)), "{{miss_reason}}", instant=True)],
    "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "namePlacement": "left", "valueMode": "color",
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""}},
    "fieldConfig": {"defaults": {"unit": "short", "decimals": 0, "min": 0, "color": {"mode": "fixed", "fixedColor": "#B7B7B7"}},
                    "overrides": reason_overrides()}})
cmr = "catalog, model, miss_reason"
panels.append({
    "type": "table", "title": "Misses by catalog, model and reason", "id": nid(), "datasource": DS,
    "description": "Start with the rows marked No aggregate created: investigate. " + REASON_HELP,
    "gridPos": {"h": 9, "w": 15, "x": 9, "y": y},
    "targets": [tgt(reason_legend('sum by (%s) (increase(%s_count{%s,%s}[$__range]))' % (cmr, Q, F, MISS)), ref="A", instant=True, fmt="table"),
                tgt(reason_legend('sum by (%s) (increase(%s_sum{%s,%s}[$__range])) / sum by (%s) (increase(%s_count{%s,%s}[$__range]))' % (cmr, Q, F, MISS, cmr, Q, F, MISS)),
                    ref="B", instant=True, fmt="table")],
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                       "renameByName": {"catalog": "Catalog", "model": "Model", "miss_reason": "Reason", "Value #A": "Queries", "Value #B": "Avg total"},
                                       "indexByName": {"catalog": 0, "model": 1, "miss_reason": 2, "Value #A": 3, "Value #B": 4}}},
        {"id": "filterByValue", "options": {"type": "exclude", "match": "any", "filters": [{"fieldName": "Queries", "config": {"id": "lowerOrEqual", "options": {"value": 0}}}]}},
        {"id": "sortBy", "options": {"sort": [{"field": "Queries", "desc": True}]}}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto"}},
                    "overrides": [{"matcher": {"id": "byName", "options": "Queries"}, "properties": [{"id": "unit", "value": "short"}, {"id": "decimals", "value": 0}]},
                                  {"matcher": {"id": "byName", "options": "Avg total"}, "properties": [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 3}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"}})
y += 9

LOKI = {"type": "loki", "uid": "${loki}"}
LOG_FIELDS = ["start", "query_id", "catalog", "model", "query_type", "optimization", "miss_reason", "status", "duration_ms",
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
                                                        "query_type": "Type", "optimization": "Optimization", "miss_reason": "Miss reason", "status": "Status",
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

ENG = ('$engine_log_selector |= "R050 candidate" '
       r'| regexp `R050 candidate (?P<candidate>\S+): score=(?P<score>[0-9.Ee+-]+) .*Dataset (?P<dataset>\S+) and (?P<grain>.*?) is ` '
       '| regexp `queryId=(?P<query_id>[0-9a-f-]+)` '
       '| label_format grain=`{{ if .grain }}{{ .grain | replace " ," ", " }}{{ else }}(no keys){{ end }}` '
       '| score >= $min_score | score <= $max_score')
SCORE_HELP = ("Compression score = rows in the fact dataset / estimated rows of the proposed aggregate, as the AtScale engine logs it "
              "for each aggregate candidate (rule R050). With aggregates.create.threshold.enabled, candidates below "
              "aggregates.create.compression.threshold (default 3.0) are not created. Read from the AtScale engine log in Loki "
              "(Engine log selector at the top); the engine log has no catalog or model, so only the time range applies.")
panels.append(row("Aggregate candidates and compression score (needs the AtScale engine log in Loki)", y)); y += 1
panels.append({
    "type": "table", "title": "Aggregate candidates by dataset and grain", "id": nid(), "datasource": LOKI,
    "description": SCORE_HELP + " Below threshold uses the Compression threshold field at the top.",
    "gridPos": {"h": 9, "w": 12, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "instant", "expr": "sum by (dataset, grain) (count_over_time(%s [$__range]))" % ENG},
                {"datasource": LOKI, "refId": "B", "queryType": "instant", "expr": "sum by (dataset, grain) (count_over_time(%s | score < $compression_threshold [$__range]))" % ENG},
                {"datasource": LOKI, "refId": "C", "queryType": "instant", "expr": "avg by (dataset, grain) (avg_over_time(%s | unwrap score [$__range]))" % ENG}],
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                       "renameByName": {"dataset": "Fact dataset", "grain": "Aggregate grain", "Value #A": "Candidates",
                                                        "Value #B": "Below threshold", "Value #C": "Compression score"},
                                       "indexByName": {"dataset": 0, "grain": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Compression score"}]}}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto"}, "decimals": 0},
                    "overrides": [
                        {"matcher": {"id": "byName", "options": "Compression score"}, "properties": [
                            {"id": "decimals", "value": 2},
                            {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 3}]}}]},
                        {"matcher": {"id": "byName", "options": "Below threshold"}, "properties": [
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": "red", "value": 1}]}}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"}})
panels.append({
    "type": "timeseries", "title": "Candidates below the compression threshold", "id": nid(), "datasource": LOKI,
    "description": "Aggregate candidates per interval whose compression score is below the Compression threshold field, by fact dataset. " + SCORE_HELP,
    "gridPos": {"h": 9, "w": 12, "x": 12, "y": y}, "interval": "5m",
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "legendFormat": "{{dataset}}",
                 "expr": "sum by (dataset) (count_over_time(%s | score < $compression_threshold [$__interval]))" % ENG}],
    "fieldConfig": {"defaults": {"unit": "short", "decimals": 0, "custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}}}, "overrides": []},
    "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
y += 9
CAND_FIELDS = ["Time", "query_id", "dataset", "grain", "score", "candidate"]
panels.append({
    "type": "table", "title": "Aggregate candidates per query", "id": nid(), "datasource": LOKI,
    "description": "One row per aggregate candidate, lowest score first. Search the Query ID in Slowest queries or on the Queries page in AtScale Design Center. " + SCORE_HELP,
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "maxLines": 1000, "expr": ENG}],
    "transformations": [
        {"id": "extractFields", "options": {"source": "labels", "format": "auto", "replace": False, "keepTime": False}},
        {"id": "filterFieldsByName", "options": {"include": {"names": CAND_FIELDS}}},
        {"id": "convertFieldType", "options": {"conversions": [{"targetField": "score", "destinationType": "number"}]}},
        {"id": "organize", "options": {"indexByName": {n: i for i, n in enumerate(CAND_FIELDS)},
                                       "renameByName": {"query_id": "Query ID", "dataset": "Fact dataset", "grain": "Aggregate grain",
                                                        "score": "Compression score", "candidate": "Candidate ID"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Compression score"}]}}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True}},
                    "overrides": [{"matcher": {"id": "byName", "options": "Compression score"}, "properties": [
                        {"id": "decimals", "value": 2},
                        {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                        {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 3}]}}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"}})
y += 10

RUNTIME_BUCKETS = [(1000, "1. under 1 s"), (5000, "2. 1 to 5 s"), (10000, "3. 5 to 10 s"), (30000, "4. 10 to 30 s"),
                   (None, "5. 30 s and more")]
SCORE_BUCKETS = [(1, "under 1"), (3, "1 to 3"), (10, "3 to 10"), (100, "10 to 100"), (None, "100 and more")]

def bucket_template(field, buckets):
    """Loki template that maps a numeric label to a bucket name."""
    parts = []
    for i, (limit, name) in enumerate(buckets):
        if limit is None:
            parts.append("{{ else }}%s{{ end }}" % name)
        else:
            parts.append("{{ %s lt (float64 .%s) %s.0 }}%s" % ("if" if i == 0 else "else if", field, limit, name))
    return "".join(parts)

EXP_DIST = ('$log_selector |= "atscale_query" | json | event="atscale_query" | env=~"$env" | query_type=~"$query_type" '
            '| catalog=~"$catalog" | model=~"$model" | optimization="none" | status="successful" '
            '| duration_ms >= $min_runtime_ms | duration_ms <= $max_runtime_ms '
            '| label_format runtime_bucket=`%s`' % bucket_template("duration_ms", RUNTIME_BUCKETS))
ENG_DIST = ENG + ' | label_format score_bucket=`%s`' % bucket_template("score", SCORE_BUCKETS)
MATRIX = "runtime_bucket\\score_bucket"
DIST_TRANSFORMS = [
    {"id": "extractFields", "options": {"source": "labels", "format": "auto", "replace": False, "keepTime": False}},
    {"id": "joinByField", "options": {"byField": "query_id", "mode": "inner"}},
    {"id": "filterFieldsByName", "options": {"include": {"pattern": "^(query_id|runtime_bucket|score_bucket)$"}}},
    {"id": "groupBy", "options": {"fields": {"runtime_bucket": {"operation": "groupby", "aggregations": []},
                                             "score_bucket": {"operation": "groupby", "aggregations": []},
                                             "query_id": {"operation": "aggregate", "aggregations": ["distinctCount"]}}}},
    {"id": "groupingToMatrix", "options": {"columnField": "score_bucket", "rowField": "runtime_bucket",
                                           "valueField": "query_id (distinctCount)", "emptyValue": "zero"}},
    {"id": "organize", "options": {"indexByName": dict({MATRIX: 0}, **{name: i + 1 for i, (_, name) in enumerate(SCORE_BUCKETS)})}},
    # the matrix names its first column "<row field>\\<column field>"; match it by pattern, not by exact name
    {"id": "renameByRegex", "options": {"regex": "^runtime_bucket.*$", "renamePattern": "Runtime"}},
    {"id": "sortBy", "options": {"sort": [{"field": "Runtime"}]}}]
DIST_HELP = ("Queries without aggregate or cache, counted by runtime (rows) and by the compression score of the aggregate "
             "candidates the engine proposed for them (columns). A query with candidates in several score ranges counts "
             "in each. Filters: Min/Max runtime (ms) and Min/Max compression score at the top. Needs both the exporter "
             "log and the AtScale engine log in Loki. " + SCORE_HELP)
panels.append({
    "type": "table", "title": "Unoptimized queries by runtime and compression score", "id": nid(), "datasource": LOKI,
    "description": DIST_HELP, "gridPos": {"h": 8, "w": 12, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "maxLines": 5000, "expr": EXP_DIST},
                {"datasource": LOKI, "refId": "B", "queryType": "range", "maxLines": 5000, "expr": ENG_DIST}],
    "transformations": DIST_TRANSFORMS,
    "fieldConfig": {"defaults": {"decimals": 0, "custom": {"align": "center", "cellOptions": {"type": "color-background", "mode": "gradient"}},
                                 "color": {"mode": "continuous-BlYlRd"}},
                    "overrides": [{"matcher": {"id": "byType", "options": "string"},
                                   "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}, {"id": "custom.align", "value": "left"},
                                                  {"id": "displayName", "value": "Runtime \\ compression score"}, {"id": "custom.minWidth", "value": 170}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"}})
panels.append({
    "type": "barchart", "title": "Unoptimized queries by runtime, stacked by compression score", "id": nid(), "datasource": LOKI,
    "description": DIST_HELP, "gridPos": {"h": 8, "w": 12, "x": 12, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "maxLines": 5000, "expr": EXP_DIST},
                {"datasource": LOKI, "refId": "B", "queryType": "range", "maxLines": 5000, "expr": ENG_DIST}],
    "transformations": DIST_TRANSFORMS,
    "options": {"orientation": "horizontal", "stacking": "normal", "showValue": "never", "groupWidth": 0.7, "barWidth": 0.9,
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi"}},
    "fieldConfig": {"defaults": {"decimals": 0, "unit": "short", "color": {"mode": "palette-classic"}}, "overrides": []}})
y += 8

SUG_FIELDS = ["start", "query_id", "catalog", "model", "duration_ms", "miss_reason", "dataset", "grain", "score",
              "uda_sml"]
# One exporter line per query and fact dataset carries that dataset's UDA; join it to the engine candidates
# on query ID + fact dataset, so each row shows only its own UDA.
JOIN_KEY = ' | label_format join_key=`{{ .query_id }}/{{ .dataset }}`'
EXP_Q = ('$log_selector |= "atscale_uda_suggestion" | json | event="atscale_uda_suggestion" | env=~"$env" '
         '| query_type=~"$query_type" | catalog=~"$catalog" | model=~"$model" '
         '| duration_ms >= $min_runtime_ms | duration_ms <= $max_runtime_ms' + JOIN_KEY)
# Only candidates below the threshold need a user-defined aggregate.
ENG_SUG = ENG + ' | score < $compression_threshold' + JOIN_KEY + ' | drop query_id, dataset'
panels.append({
    "type": "table", "title": "Aggregate suggestions for slow queries", "id": nid(), "datasource": LOKI,
    "description": ("Queries that ran at least Min runtime (ms), joined on the query ID with the aggregate candidates the engine "
                    "proposed for them that are below the Compression threshold. AtScale does not create those. One row per query "
                    "and fact dataset; UDA (SML) holds the aggregates: block for that fact dataset, at the grain of the query "
                    "(its attributes), with the base metrics behind its measures; comments name what was left out. Candidates that meet the threshold are in Aggregate candidates per query. Check before you create it: the query's attributes must be coarser than the rejected grain "
                    "(for example Fiscal Quarter instead of Day), and calculated measures must be replaced by the base metrics "
                    "they use. Queries that used an aggregate have no candidates and do not appear. " + SCORE_HELP),
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "maxLines": 1000, "expr": EXP_Q},
                {"datasource": LOKI, "refId": "B", "queryType": "range", "maxLines": 1000, "expr": ENG_SUG}],
    "transformations": [
        {"id": "extractFields", "options": {"source": "labels", "format": "auto", "replace": False, "keepTime": False}},
        {"id": "joinByField", "options": {"byField": "join_key", "mode": "inner"}},
        {"id": "filterFieldsByName", "options": {"include": {"pattern": "^(%s)$" % "|".join(SUG_FIELDS)}}},
        {"id": "convertFieldType", "options": {"conversions": [{"targetField": "duration_ms", "destinationType": "number"},
                                                               {"targetField": "score", "destinationType": "number"}]}},
        # the engine logs a candidate once per query part: one row per query, dataset and grain
        {"id": "groupBy", "options": {"fields": dict(
            {n: {"operation": "groupby", "aggregations": []} for n in SUG_FIELDS if n not in ("duration_ms", "score")},
            duration_ms={"operation": "aggregate", "aggregations": ["max"]},
            score={"operation": "aggregate", "aggregations": ["min"]})}},
        {"id": "organize", "options": {"indexByName": {(n + " (max)" if n == "duration_ms" else n + " (min)" if n == "score" else n): i
                                                       for i, n in enumerate(SUG_FIELDS)},
                                       "renameByName": {"start": "Start (UTC)", "query_id": "Query ID", "catalog": "Catalog", "model": "Model",
                                                        "duration_ms (max)": "Runtime", "miss_reason": "Miss reason", "dataset": "Fact dataset",
                                                        "grain": "Candidate grain", "score (min)": "Compression score",
                                                        "uda_sml": "UDA (SML)"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Runtime", "desc": True}]}}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True}},
                    "overrides": [
                        {"matcher": {"id": "byName", "options": "Runtime"}, "properties": [{"id": "unit", "value": "ms"}, {"id": "decimals", "value": 0},
                            {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "gradient"}}, {"id": "color", "value": {"mode": "continuous-GrYlRd"}}]},
                        {"matcher": {"id": "byName", "options": "Compression score"}, "properties": [{"id": "decimals", "value": 2},
                            {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 3}]}}]},
                        {"matcher": {"id": "byName", "options": "UDA (SML)"}, "properties": [
                            {"id": "custom.minWidth", "value": 460}, {"id": "custom.inspect", "value": True},
                            {"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"}})
y += 12

QUEUE = ('$engine_log_selector |= "going into queue" '
         '| regexp `conn=(?P<connection>[^,}]+)` | regexp `queryId=(?P<query_id>[0-9a-f-]+)` '
         r'| regexp `is \[#(?P<position>\d+)\] in line` | regexp `service=(?P<service>[^,}]+)` '
         '| label_format requester=`{{ if .service }}{{ .service }}{{ else }}User query{{ end }}`')
QUEUE_HELP = ("Each warehouse query asks the AtScale connection manager for a connection to the data warehouse. The engine logs "
              "the request with its place in line: #1 means first in line, a higher place means the request waited behind "
              "others. User queries share the connection with AtScale's own services (statistics, aggregate builds, "
              "health checks). Deep queues explain a high Outbound wait. Read from the AtScale engine log in Loki (Engine "
              "log selector at the top); only the time range applies.")
panels.append(row("Warehouse connection queue (needs the AtScale engine log in Loki)", y)); y += 1
panels.append({
    "type": "timeseries", "title": "Deepest place in the warehouse connection queue", "id": nid(), "datasource": LOKI,
    "description": "Highest queue place per interval and connection. 1 means no request waited. " + QUEUE_HELP,
    "gridPos": {"h": 8, "w": 12, "x": 0, "y": y}, "interval": "5m",
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "legendFormat": "{{connection}}",
                 "expr": "max by (connection) (max_over_time(%s | unwrap position [$__interval]))" % QUEUE}],
    "fieldConfig": {"defaults": {"unit": "short", "decimals": 0, "min": 1, "custom": {"drawStyle": "bars", "fillOpacity": 70},
                                 "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 2}]},
                                 "color": {"mode": "thresholds"}}, "overrides": []},
    "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
panels.append({
    "type": "timeseries", "title": "Requests that queued behind others, by requester", "id": nid(), "datasource": LOKI,
    "description": "Warehouse connection requests with a queue place above 1, per interval, by who asked (User query or an AtScale service). " + QUEUE_HELP,
    "gridPos": {"h": 8, "w": 12, "x": 12, "y": y}, "interval": "5m",
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "legendFormat": "{{requester}}",
                 "expr": "sum by (requester) (count_over_time(%s | position > 1 [$__interval]))" % QUEUE}],
    "fieldConfig": {"defaults": {"unit": "short", "decimals": 0, "custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}}},
                    "overrides": [{"matcher": {"id": "byName", "options": "User query"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#F2495C"}}]}]},
    "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}}})
y += 8
panels.append({
    "type": "table", "title": "Warehouse connection queue by connection and requester", "id": nid(), "datasource": LOKI,
    "description": QUEUE_HELP,
    "gridPos": {"h": 8, "w": 24, "x": 0, "y": y},
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "instant", "expr": "sum by (connection, requester) (count_over_time(%s [$__range]))" % QUEUE},
                {"datasource": LOKI, "refId": "B", "queryType": "instant", "expr": "sum by (connection, requester) (count_over_time(%s | position > 1 [$__range]))" % QUEUE},
                {"datasource": LOKI, "refId": "C", "queryType": "instant", "expr": "max by (connection, requester) (max_over_time(%s | unwrap position [$__range]))" % QUEUE}],
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                       "renameByName": {"connection": "Connection", "requester": "Requester", "Value #A": "Requests",
                                                        "Value #B": "Queued behind others", "Value #C": "Deepest place"},
                                       "indexByName": {"connection": 0, "requester": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Queued behind others", "desc": True}]}}],
    "fieldConfig": {"defaults": {"decimals": 0, "custom": {"align": "auto"}},
                    "overrides": [{"matcher": {"id": "byName", "options": "Deepest place"}, "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                        {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": "red", "value": 2}]}}]}]},
    "options": {"showHeader": True, "cellHeight": "sm"}})
y += 8

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
        {"name": "engine_log_selector", "label": "Engine log selector", "type": "textbox", "query": '{container="engine"}',
         "current": {"text": '{container="engine"}', "value": '{container="engine"}'}, "hide": 0},
        {"name": "compression_threshold", "label": "Compression threshold", "type": "textbox", "query": "3",
         "current": {"text": "3", "value": "3"}, "hide": 0},
        {"name": "min_runtime_ms", "label": "Min runtime (ms)", "type": "textbox", "query": "1000",
         "current": {"text": "1000", "value": "1000"}, "hide": 0},
        {"name": "max_runtime_ms", "label": "Max runtime (ms)", "type": "textbox", "query": "3600000",
         "current": {"text": "3600000", "value": "3600000"}, "hide": 0},
        {"name": "min_score", "label": "Min compression score", "type": "textbox", "query": "0",
         "current": {"text": "0", "value": "0"}, "hide": 0},
        {"name": "max_score", "label": "Max compression score", "type": "textbox", "query": "1000000",
         "current": {"text": "1000000", "value": "1000000"}, "hide": 0},
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
