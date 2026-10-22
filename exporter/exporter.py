"""AtScale query lifecycle exporter for Prometheus.

Sample code, provided as is under the MIT License. Not an AtScale product and not
supported by AtScale. See DISCLAIMER.md in the repository root.

Purpose: AtScale records the lifecycle of each query (Inbound Query, Planning, Outbound
  with its warehouse subqueries, Result Processing) in its query history. This exporter
  polls the query history API of one or more AtScale instances and publishes the phase
  durations as Prometheus histograms, so Grafana can show averages and percentiles per
  phase, catalog and model.
Inputs (environment variables):
  ATSCALE_INSTANCES      comma list of name=base_url, e.g. prod=https://atscale.example.com
  ATSCALE_TOKEN_<NAME>   AtScale API token per instance; NAME is the instance name in upper
                         case with non-alphanumeric characters replaced by "_"
  QUERY_PATH             query history path (default /v1/query; /v1/public/query when you
                         call the sml-api service directly inside Kubernetes)
  CA_BUNDLE              optional PEM file with extra CA certificates (self-signed or
                         private CA); certificate verification is always on
  AGGREGATES_PATH        aggregates path (default: QUERY_PATH with "query" replaced by
                         "aggregates", i.e. /v1/aggregates or /v1/public/aggregates)
  CATALOGS_PATH          catalogs path for the catalog export (default: QUERY_PATH with "query"
                         replaced by "catalogs"); used for the UDA suggestion
  CATALOG_CACHE_MINUTES  how long a catalog export is reused (60)
  POLL_SECONDS (60), LOOKBACK_MINUTES (30), PAGE_SIZE (100, the API maximum), MAX_PAGES (20),
  HTTP_TIMEOUT_SECONDS (30), PORT (9101)
  QUERY_LOG              true (default) writes one JSON line per query to stdout
Outputs: Prometheus text format on http://0.0.0.0:$PORT/metrics; with QUERY_LOG, one JSON
  line per query on stdout (event "atscale_query": queryId, catalog, model, attribute and
  measure names, phase ms) for a log store such as Loki. For queries that reached the
  warehouse without aggregate or cache it adds uda_sml: an SML "aggregates:" block with the
  query's attributes (and their dimensions) and measures, to paste into the model file.
  User names and query text are never written.
Run: python3 exporter.py

Each query gets a miss_reason: optimized (aggregate or cache used), system (AtScale's own
queries, for example aggregate builds and statistics), failed, no_warehouse
(answered without a warehouse query), aggregate_created (AtScale created an aggregate for
the model while the query ran, so the next run of this footprint can use it),
no_aggregate_created (investigate: for example an exact distinct count), or unclassified
(the aggregates API could not be read).

Each query is counted once (deduplicated on queryId). Queries already in the history
when the exporter starts are not counted, so a restart does not count them twice.
Run exactly one replica: two replicas would count every query twice.
"""
import datetime
import hashlib
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("atscale-query-exporter")
QUERY_LOG = logging.getLogger("atscale-query-exporter.queries")

# Seconds. Inbound and planning are usually 1 to 100 ms, outbound is often seconds.
BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)
SUBQUERY_BUCKETS = (0, 1, 2, 3, 5, 10, 20, 50)

PHASE_NAMES = {
    "Inbound Query": "inbound",
    "Planning": "planning",
    "Outbound": "outbound",
    "Outbound Queries": "outbound",
    "Result Processing": "result_processing",
}
QUERY_LABELS = ("env", "catalog", "model", "status", "query_type", "optimization", "dialect", "miss_reason")
MAX_API_PAGE_SIZE = 100       # the AtScale API returns at most 101 rows and no next page above this
# An aggregate created from 5 s before the query start to 10 s after its end
# counts as created for this query.
AGG_WINDOW_BEFORE_MS, AGG_WINDOW_AFTER_MS = 5000, 10000
ENGINE_HELPER_PREFIX = "__"   # internal measures such as __SUMATTRNAME__..., not part of the model
MEASURE_REF = re.compile(r"\[Measures\]\.\[([^\]]+)\]", re.IGNORECASE)


class Histogram:
    """Minimal thread-safe Prometheus histogram keyed by a label tuple."""

    def __init__(self, name, help_text, label_names, buckets):
        self.name, self.help, self.label_names, self.buckets = name, help_text, label_names, buckets
        self._series = {}
        self._lock = threading.Lock()

    def observe(self, labels, value):
        with self._lock:
            counts, total = self._series.get(labels, ([0] * len(self.buckets), [0, 0.0]))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1
            total[0] += 1
            total[1] += value
            self._series[labels] = (counts, total)

    def render(self):
        lines = ["# HELP %s %s" % (self.name, self.help), "# TYPE %s histogram" % self.name]
        with self._lock:
            for labels, (counts, total) in sorted(self._series.items()):
                base = fmt_labels(self.label_names, labels)
                for bound, count in zip(self.buckets, counts):
                    lines.append('%s_bucket{%s,le="%s"} %d' % (self.name, base, bound, count))
                lines.append('%s_bucket{%s,le="+Inf"} %d' % (self.name, base, total[0]))
                lines.append("%s_count{%s} %d" % (self.name, base, total[0]))
                lines.append("%s_sum{%s} %.6f" % (self.name, base, total[1]))
        return lines


class Gauges:
    """Per-instance gauges and counters about the exporter itself."""

    def __init__(self):
        self.values = {}
        self._lock = threading.Lock()

    def set(self, metric, env, value):
        with self._lock:
            self.values[(metric, env)] = value

    def inc(self, metric, env):
        with self._lock:
            self.values[(metric, env)] = self.values.get((metric, env), 0) + 1

    def render(self):
        with self._lock:
            return ['%s{env="%s"} %s' % (m, escape(e), v) for (m, e), v in sorted(self.values.items())]


QUERY_HIST = Histogram("atscale_query_duration_seconds",
                       "End-to-end server duration of an AtScale query.", QUERY_LABELS, BUCKETS)
PHASE_HIST = Histogram("atscale_query_phase_duration_seconds",
                       "Duration of one lifecycle phase of an AtScale query. warehouse = wall time "
                       "covered by subqueries, outbound_wait = outbound minus warehouse.",
                       QUERY_LABELS + ("phase",), BUCKETS)
SUBQ_HIST = Histogram("atscale_query_subqueries",
                      "Number of outbound warehouse subqueries per AtScale query.",
                      QUERY_LABELS, SUBQUERY_BUCKETS)
SELF = Gauges()


def fmt_labels(names, values):
    return ",".join('%s="%s"' % (n, escape(v)) for n, v in zip(names, values))


def escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_") or "unknown"


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def optimization_of(rec):
    optimization = rec.get("optimization") or []
    if isinstance(optimization, str):
        optimization = [optimization]
    return "+".join(sorted(optimization)) or "none"


def classify(rec, subquery_count, created_ms):
    """Why a query used no aggregate or cache. created_ms: creation times (epoch ms) of the
    aggregates of the query's model, or None if they could not be read."""
    if optimization_of(rec) != "none":
        return "optimized"
    if rec.get("queryType") == "System":
        return "system"
    if rec.get("status") != "successful":
        return "failed"
    if subquery_count == 0:
        return "no_warehouse"
    if created_ms is None or not is_num(rec.get("startTime")):
        return "unclassified"
    start = rec["startTime"]
    end = start + (rec["duration"] if is_num(rec.get("duration")) else 0)
    if any(start - AGG_WINDOW_BEFORE_MS <= t <= end + AGG_WINDOW_AFTER_MS for t in created_ms):
        return "aggregate_created"
    return "no_aggregate_created"


def needs_aggregate_check(rec):
    """True for a successful query that reached the warehouse without aggregate or cache."""
    return (optimization_of(rec) == "none" and rec.get("status") == "successful"
            and rec.get("queryType") != "System" and phase_durations(rec)[1] > 0)


def query_labels(env, rec, miss_reason="unclassified"):
    return (env,
            rec.get("catalogName") or "unknown",
            rec.get("modelName") or "unknown",
            rec.get("status") or "unknown",
            rec.get("queryType") or "unknown",
            optimization_of(rec),
            rec.get("dialect") or "unknown",
            miss_reason)


def interval_union_ms(subqueries):
    """Wall time covered by possibly parallel subqueries, in ms."""
    spans = sorted((float(s["startTime"]), float(s["startTime"]) + float(s["duration"]))
                   for s in subqueries if is_num(s.get("startTime")) and is_num(s.get("duration")))
    covered, cur_start, cur_end = 0.0, None, None
    for start, end in spans:
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                covered += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        covered += cur_end - cur_start
    return covered


def phase_durations(rec):
    """Return {phase: seconds} for one query record, plus the subquery count."""
    out, subquery_count = {}, 0
    for event in rec.get("events") or []:
        if not is_num(event.get("duration")):
            continue
        phase = PHASE_NAMES.get(event.get("name"), slug(event.get("name")))
        out[phase] = out.get(phase, 0.0) + event["duration"] / 1000.0
        if phase == "outbound":
            subqueries = event.get("subqueries") or []
            subquery_count += len(subqueries)
            warehouse_ms = interval_union_ms(subqueries)
            out["warehouse"] = out.get("warehouse", 0.0) + warehouse_ms / 1000.0
            out["outbound_wait"] = (out.get("outbound_wait", 0.0)
                                    + max(0.0, event["duration"] - warehouse_ms) / 1000.0)
    return out, subquery_count


def record(env, rec, created_ms=None, model_index=None):
    """Observe one query record from the query history API."""
    phases, subquery_count = phase_durations(rec)
    labels = query_labels(env, rec, classify(rec, subquery_count, created_ms))
    if is_num(rec.get("duration")):
        QUERY_HIST.observe(labels, rec["duration"] / 1000.0)
    for phase, seconds in phases.items():
        PHASE_HIST.observe(labels + (phase,), seconds)
    SUBQ_HIST.observe(labels, subquery_count)
    if QUERY_LOG.isEnabledFor(logging.INFO):
        line = query_log_line(labels, rec, phases, subquery_count)
        if model_index is not None and needs_aggregate_check(rec):
            line["uda_sml"] = uda_sml(rec, model_index)
        QUERY_LOG.info(json.dumps(line, sort_keys=True))
    return bool(phases)


def query_log_line(labels, rec, phases, subquery_count):
    """One JSON object per query for drill-down in a log store. Attribute and measure names
    are model object names; user names and query text are never written."""
    line = dict(zip(QUERY_LABELS, labels))
    line.update({"event": "atscale_query", "query_id": rec.get("queryId"),
                 "start": iso(rec["startTime"] / 1000.0) if is_num(rec.get("startTime")) else None,
                 "duration_ms": round(rec["duration"], 1) if is_num(rec.get("duration")) else None,
                 "subqueries": subquery_count,
                 "attributes": ", ".join(sorted(rec.get("attributes") or [])),
                 "measures": ", ".join(sorted(rec.get("measures") or []))})
    line.update({"%s_ms" % phase: round(seconds * 1000.0, 1) for phase, seconds in phases.items()})
    return line


def as_list(value):
    """The catalog export writes single-element collections as bare objects and empty ones as ""."""
    if isinstance(value, list):
        return value
    return [] if value in (None, "") else [value]


def referenced_ids(node):
    """All string values in a nested export structure (ids that point at attributes)."""
    if isinstance(node, dict):
        for value in node.values():
            yield from referenced_ids(value)
    elif isinstance(node, list):
        for value in node:
            yield from referenced_ids(value)
    elif isinstance(node, str):
        yield node


def catalog_index(doc):
    """{model name: index} from a catalog export. An index maps attributes to dimensions,
    base metrics to their fact dataset, calculations to the measures they reference, and
    dimensions and datasets to the key ids that relate them."""
    keyed = {a["id"]: a for a in as_list((doc.get("attributes") or {}).get("keyed-attribute"))}
    dataset_names = {d["id"]: d["name"] for d in as_list((doc.get("data-sets") or {}).get("data-set"))}
    calcs = {c["name"]: MEASURE_REF.findall(c.get("expression") or "")
             for c in as_list((doc.get("calculated-members") or {}).get("calculated-member"))}
    shared_dims = as_list((doc.get("dimensions") or {}).get("dimension"))
    out = {}
    for cube in as_list((doc.get("cubes") or {}).get("cube")):
        cube_keyed = dict(keyed, **{a["id"]: a for a in as_list((cube.get("attributes") or {}).get("keyed-attribute"))})
        index = dimension_index(shared_dims + as_list((cube.get("dimensions") or {}).get("dimension")), cube_keyed)
        index.update(dataset_index(cube, dataset_names))
        index["calcs"] = calcs
        out[cube["name"]] = index
        caption = (cube.get("properties") or {}).get("caption")
        if caption:
            out.setdefault(caption, index)
    return out


def dimension_index(dims, keyed):
    """attr_dims {attribute: [dimensions]}, dim_keys {dimension: key ids}, calc_groups {names}."""
    attr_dims, dim_keys, calc_groups = {}, {}, set()
    for dim in dims:
        calc_groups.update(g.get("name") for g in as_list(dim.get("calculation-group")))
        for ref in referenced_ids(as_list(dim.get("hierarchy"))):
            attr = keyed.get(ref)
            if not attr:
                continue
            dim_keys.setdefault(dim["name"], set()).add(attr.get("key-ref"))
            dims_of_attr = attr_dims.setdefault(attr["name"], [])
            if dim["name"] not in dims_of_attr:
                dims_of_attr.append(dim["name"])
    return {"attr_dims": attr_dims, "dim_keys": dim_keys, "calc_groups": calc_groups}


def dataset_index(cube, dataset_names):
    """metric_dataset {base metric: fact dataset} and dataset_keys {fact dataset: key ids}."""
    metric_ids = {a["id"]: a["name"] for a in as_list((cube.get("attributes") or {}).get("attribute"))}
    metric_dataset, dataset_keys = {}, {}
    for ref in as_list((cube.get("data-sets") or {}).get("data-set-ref")):
        name = dataset_names.get(ref.get("id"), ref.get("id"))
        logical = ref.get("logical") or {}
        dataset_keys[name] = {k.get("id") for k in as_list(logical.get("key-ref"))}
        for attr in as_list(logical.get("attribute-ref")):
            if attr.get("id") in metric_ids:
                metric_dataset[metric_ids[attr["id"]]] = name
    return {"metric_dataset": metric_dataset, "dataset_keys": dataset_keys}


def yaml_str(value):
    """A YAML scalar: plain when safe, otherwise double-quoted."""
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9 _./()%-]*[A-Za-z0-9_)%]|[A-Za-z0-9_]", text) and ": " not in text:
        return text
    return json.dumps(text)


def base_metrics(measure, index, seen=None):
    """The base metrics a measure uses: itself, or the metrics behind a calculation (recursive)."""
    seen = set() if seen is None else seen
    if measure in seen:
        return set()
    seen.add(measure)
    if measure in index["metric_dataset"]:
        return {measure}
    out = set()
    for ref in index["calcs"].get(measure, []):
        out |= base_metrics(ref, index, seen)
    return out


def uda_sml(rec, index):
    """SML "aggregates:" block for the query: one UDA per fact dataset with the base metrics the
    query's measures use and the query's attributes related to that dataset; notes as comments."""
    measures = sorted(m for m in rec.get("measures") or [] if not m.startswith(ENGINE_HELPER_PREFIX))
    metrics, notes = set(), []
    for measure in measures:
        found = base_metrics(measure, index)
        metrics |= found
        if not found:
            notes.append("# skipped %s: no base metric found in the model" % measure)
    if not metrics:
        return "\n".join(notes + ["# no UDA: the query has no measures, AtScale answers it from the dimension tables"])
    attributes, attr_notes = uda_attributes(sorted(rec.get("attributes") or []), index)
    by_dataset = {}
    for metric in sorted(metrics):
        by_dataset.setdefault(index["metric_dataset"][metric], []).append(metric)
    lines = ["aggregates:"]
    for dataset, dataset_metrics in sorted(by_dataset.items()):
        related, unrelated = split_related(attributes, dataset, index)
        attr_notes += ["# left out %s for %s: %s has no relationship to it" % (a, dataset, d) for a, d in unrelated]
        lines += uda_entry(rec.get("modelName"), dataset, related, dataset_metrics)
    return "\n".join(notes + attr_notes + lines)


def split_related(attributes, dataset, index):
    """Attributes whose dimension shares a key with the dataset, and the others."""
    keys = index["dataset_keys"].get(dataset, set())
    related = [(a, d) for a, d in attributes if index["dim_keys"].get(d, set()) & keys]
    return related, [(a, d) for a, d in attributes if (a, d) not in related]


def uda_entry(model, dataset, attributes, metrics):
    """YAML lines of one aggregate in an SML model file."""
    digest = hashlib.sha1(json.dumps([model, dataset, attributes, metrics]).encode()).hexdigest()[:8]
    lines = ["  - unique_name: uda_%s" % digest,
             "    label: %s" % yaml_str("Suggested on %s: %s" % (dataset, ", ".join(a for a, _ in attributes) or "grand total"))]
    if attributes:
        lines.append("    attributes:")
        for attr, dim in attributes:
            lines += ["      - name: %s" % yaml_str(attr), "        dimension: %s" % yaml_str(dim)]
    return lines + ["    metrics:"] + ["      - %s" % yaml_str(m) for m in metrics]


def uda_attributes(attrs, index):
    """[(attribute, dimension)] for the UDA, plus comment lines for what was left out or is ambiguous."""
    out, notes = [], []
    for attr in attrs:
        dims = index["attr_dims"].get(attr, [])
        if attr in index["calc_groups"]:
            notes.append("# skipped %s: calculation group" % attr)
        elif not dims:
            notes.append("# skipped %s: not found as a dimension attribute in the model" % attr)
        else:
            out.append((attr, dims[0]))
            if len(dims) > 1:
                notes.append("# check %s: also in %s" % (attr, ", ".join(dims[1:])))
    return out, notes


class InstancePoller:
    """Polls one AtScale instance and records each new query once."""

    def __init__(self, env, base_url, token, cfg):
        self.env, self.base_url, self.token, self.cfg = env, base_url.rstrip("/"), token, cfg
        self.seen = {}          # queryId -> expiry epoch seconds
        self.warmed_up = False
        self.warned_no_events = False
        self.catalogs = {}      # catalogId -> (expiry epoch seconds, {model name: index})

    def fetch(self):
        now = time.time()
        params = {"showCanaries": "false", "sort": "-startTime",
                  "startDate": iso(now - self.cfg["lookback_s"]), "endDate": iso(now + 60)}
        return self.get_paged(self.cfg["query_path"], params, "results")

    def get_paged(self, path, params, key):
        """Read all pages of a list endpoint ({key: [...], hasNextPage} or {key: [...], total})."""
        items = []
        for page in range(1, self.cfg["max_pages"] + 1):
            query = dict(params, page=page, size=self.cfg["page_size"])
            doc = self.get_json(path + "?" + urllib.parse.urlencode(query))
            batch = doc.get(key) if isinstance(doc, dict) else None
            if batch is None:
                raise ValueError("%s: response has no '%s' list; check the path and the AtScale version" % (path, key))
            items.extend(batch)
            more = doc.get("hasNextPage") if "hasNextPage" in doc else len(items) < int(doc.get("total") or 0)
            if not more or not batch:
                return items
        LOG.warning("%s: %s stopped at %d pages; raise MAX_PAGES", self.env, path, self.cfg["max_pages"])
        return items

    def aggregate_creation_times(self, model_ids):
        """{modelId: [creation epoch ms]} of the models' aggregates, or None if unreadable."""
        try:
            out = {}
            for model_id in model_ids:
                aggs = self.get_paged(self.cfg["aggregates_path"], {"modelId": model_id}, "data")
                out[model_id] = [parse_ms((a.get("stats") or {}).get("createdAt")) for a in aggs]
            return out
        except (OSError, ValueError) as exc:
            LOG.warning("%s: aggregates not readable, misses stay unclassified: %s", self.env, exc)
            return None

    def get_json(self, path):
        req = urllib.request.Request(self.base_url + path, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.cfg["timeout_s"], context=self.cfg["ssl"]) as resp:
            return json.load(resp)

    def poll(self):
        now = time.time()
        fresh = []
        for rec in self.fetch():
            qid = rec.get("queryId")
            if not qid or qid in self.seen:
                continue
            self.seen[qid] = now + 2 * self.cfg["lookback_s"]
            fresh.append(rec)
        self.seen = {q: exp for q, exp in self.seen.items() if exp > now}
        new = len(fresh) if self.warmed_up else 0
        without_events = self.record_all(fresh) if self.warmed_up else 0
        if not self.warmed_up:
            LOG.info("%s: warm-up, %d existing queries skipped", self.env, len(self.seen))
        self.warmed_up = True
        self.check_fields(new, without_events)
        return new

    def record_all(self, records):
        """Record new queries; returns how many had no phase events."""
        misses = [r for r in records if needs_aggregate_check(r)]
        models = {r.get("modelId") for r in misses if r.get("modelId")}
        created = self.aggregate_creation_times(sorted(models)) if models else {}
        indexes = {c: self.model_index(c) for c in {r.get("catalogId") for r in misses if r.get("catalogId")}}
        without_events = 0
        for rec in records:
            times = None if created is None else created.get(rec.get("modelId"), [])
            index = (indexes.get(rec.get("catalogId")) or {}).get(rec.get("modelName"))
            without_events += 0 if record(self.env, rec, times, index) else 1
        return without_events

    def model_index(self, catalog_id):
        """{model name: index} from the catalog export, cached; {} if the export is unreadable."""
        expiry, index = self.catalogs.get(catalog_id, (0, None))
        if time.time() < expiry:
            return index
        try:
            doc = self.get_json("%s/%s/export" % (self.cfg["catalogs_path"], urllib.parse.quote(catalog_id)))
            index = catalog_index(doc)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("%s: catalog export %s not readable, no UDA suggestion: %s", self.env, catalog_id, exc)
            index = {}
        self.catalogs[catalog_id] = (time.time() + self.cfg["catalog_cache_s"], index)
        return index

    def check_fields(self, new, without_events):
        """Warn once if this AtScale version returns queries without phase events."""
        if new and without_events == new and not self.warned_no_events:
            LOG.warning("%s: %d queries had no 'events'; this AtScale version may not expose "
                        "the phase split, only total durations are recorded", self.env, new)
            self.warned_no_events = True


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def parse_ms(text):
    """ISO 8601 timestamp (as the AtScale API returns it) to epoch ms; 0 if missing."""
    if not text:
        return 0
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000.0


def poll_forever(pollers, poll_s):
    while True:
        for p in pollers:
            try:
                new = p.poll()
                SELF.set("atscale_query_exporter_up", p.env, 1)
                SELF.set("atscale_query_exporter_last_success_timestamp_seconds", p.env, int(time.time()))
                LOG.info("%s: %d new queries", p.env, new)
            except (OSError, ValueError) as exc:   # URLError/HTTPError are OSError; bad JSON is ValueError
                SELF.set("atscale_query_exporter_up", p.env, 0)
                SELF.inc("atscale_query_exporter_poll_errors_total", p.env)
                LOG.warning("%s: poll failed: %s", p.env, exc)
        time.sleep(poll_s)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_error(404)
            return
        lines = QUERY_HIST.render() + PHASE_HIST.render() + SUBQ_HIST.render() + SELF.render()
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def token_var(name):
    return "ATSCALE_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def load_config():
    ca_bundle = os.environ.get("CA_BUNDLE")
    query_path = os.environ.get("QUERY_PATH", "/v1/query")
    page_size = int(os.environ.get("PAGE_SIZE", str(MAX_API_PAGE_SIZE)))
    if page_size > MAX_API_PAGE_SIZE:
        LOG.warning("PAGE_SIZE %d is above the API maximum; using %d", page_size, MAX_API_PAGE_SIZE)
        page_size = MAX_API_PAGE_SIZE
    return {"query_path": query_path,
            "aggregates_path": os.environ.get("AGGREGATES_PATH", re.sub(r"query$", "aggregates", query_path)),
            "catalogs_path": os.environ.get("CATALOGS_PATH", re.sub(r"query$", "catalogs", query_path)),
            "catalog_cache_s": int(os.environ.get("CATALOG_CACHE_MINUTES", "60")) * 60,
            "lookback_s": int(os.environ.get("LOOKBACK_MINUTES", "30")) * 60,
            "page_size": page_size,
            "max_pages": int(os.environ.get("MAX_PAGES", "20")),
            "timeout_s": int(os.environ.get("HTTP_TIMEOUT_SECONDS", "30")),
            "ssl": ssl.create_default_context(cafile=ca_bundle or None)}


def load_pollers(cfg):
    pollers = []
    for item in filter(None, (i.strip() for i in os.environ.get("ATSCALE_INSTANCES", "").split(","))):
        env, _, url = item.partition("=")
        token = os.environ.get(token_var(env))
        if not env or not url.startswith(("http://", "https://")):
            raise SystemExit("ATSCALE_INSTANCES entry %r must be name=http(s)://host" % item)
        if not token:
            raise SystemExit("missing %s for instance %r" % (token_var(env), env))
        pollers.append(InstancePoller(env, url, token, cfg))
    if not pollers:
        raise SystemExit("ATSCALE_INSTANCES is empty")
    return pollers


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))   # bare JSON, parseable by the log store
    QUERY_LOG.addHandler(handler)
    QUERY_LOG.propagate = False
    enabled = os.environ.get("QUERY_LOG", "true").lower() in ("1", "true", "yes")
    QUERY_LOG.setLevel(logging.INFO if enabled else logging.WARNING)


def main():
    setup_logging()
    pollers = load_pollers(load_config())
    threading.Thread(target=poll_forever, daemon=True,
                     args=(pollers, int(os.environ.get("POLL_SECONDS", "60")))).start()
    port = int(os.environ.get("PORT", "9101"))
    LOG.info("serving /metrics on :%d for %s", port, ", ".join(p.env for p in pollers))
    ThreadingHTTPServer(("0.0.0.0", port), MetricsHandler).serve_forever()


if __name__ == "__main__":
    main()
