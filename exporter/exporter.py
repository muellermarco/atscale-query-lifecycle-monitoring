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
  POLL_SECONDS (60), LOOKBACK_MINUTES (30), PAGE_SIZE (500), MAX_PAGES (20),
  HTTP_TIMEOUT_SECONDS (30), PORT (9101)
Outputs: Prometheus text format on http://0.0.0.0:$PORT/metrics
Run: python3 exporter.py

Each query is counted once (deduplicated on queryId). Queries already in the history
when the exporter starts are not counted, so a restart does not count them twice.
Run exactly one replica: two replicas would count every query twice.
"""
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
QUERY_LABELS = ("env", "catalog", "model", "status", "query_type", "optimization", "dialect")


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


def query_labels(env, rec):
    optimization = rec.get("optimization") or []
    if isinstance(optimization, str):
        optimization = [optimization]
    return (env,
            rec.get("catalogName") or "unknown",
            rec.get("modelName") or "unknown",
            rec.get("status") or "unknown",
            rec.get("queryType") or "unknown",
            "+".join(sorted(optimization)) or "none",
            rec.get("dialect") or "unknown")


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


def record(env, rec):
    """Observe one query record from the query history API."""
    labels = query_labels(env, rec)
    if is_num(rec.get("duration")):
        QUERY_HIST.observe(labels, rec["duration"] / 1000.0)
    phases, subquery_count = phase_durations(rec)
    for phase, seconds in phases.items():
        PHASE_HIST.observe(labels + (phase,), seconds)
    SUBQ_HIST.observe(labels, subquery_count)
    return bool(phases)


class InstancePoller:
    """Polls one AtScale instance and records each new query once."""

    def __init__(self, env, base_url, token, cfg):
        self.env, self.base_url, self.token, self.cfg = env, base_url.rstrip("/"), token, cfg
        self.seen = {}          # queryId -> expiry epoch seconds
        self.warmed_up = False
        self.warned_no_events = False

    def fetch(self):
        now = time.time()
        params = {"size": self.cfg["page_size"], "showCanaries": "false", "sort": "-startTime",
                  "startDate": iso(now - self.cfg["lookback_s"]), "endDate": iso(now + 60)}
        records = []
        for page in range(1, self.cfg["max_pages"] + 1):
            params["page"] = page
            doc = self.get_json(self.cfg["query_path"] + "?" + urllib.parse.urlencode(params))
            batch = doc.get("results") if isinstance(doc, dict) else None
            if batch is None:
                raise ValueError("response has no 'results' list; check QUERY_PATH and the AtScale version")
            records.extend(batch)
            if not doc.get("hasNextPage") or not batch:
                return records
        LOG.warning("%s: stopped at %d pages; raise MAX_PAGES", self.env, self.cfg["max_pages"])
        return records

    def get_json(self, path):
        req = urllib.request.Request(self.base_url + path, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.cfg["timeout_s"], context=self.cfg["ssl"]) as resp:
            return json.load(resp)

    def poll(self):
        now = time.time()
        new = without_events = 0
        for rec in self.fetch():
            qid = rec.get("queryId")
            if not qid or qid in self.seen:
                continue
            self.seen[qid] = now + 2 * self.cfg["lookback_s"]
            if self.warmed_up:
                new += 1
                without_events += 0 if record(self.env, rec) else 1
        self.seen = {q: exp for q, exp in self.seen.items() if exp > now}
        if not self.warmed_up:
            LOG.info("%s: warm-up, %d existing queries skipped", self.env, len(self.seen))
        self.warmed_up = True
        self.check_fields(new, without_events)
        return new

    def check_fields(self, new, without_events):
        """Warn once if this AtScale version returns queries without phase events."""
        if new and without_events == new and not self.warned_no_events:
            LOG.warning("%s: %d queries had no 'events'; this AtScale version may not expose "
                        "the phase split, only total durations are recorded", self.env, new)
            self.warned_no_events = True


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


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
    return {"query_path": os.environ.get("QUERY_PATH", "/v1/query"),
            "lookback_s": int(os.environ.get("LOOKBACK_MINUTES", "30")) * 60,
            "page_size": int(os.environ.get("PAGE_SIZE", "500")),
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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pollers = load_pollers(load_config())
    threading.Thread(target=poll_forever, daemon=True,
                     args=(pollers, int(os.environ.get("POLL_SECONDS", "60")))).start()
    port = int(os.environ.get("PORT", "9101"))
    LOG.info("serving /metrics on :%d for %s", port, ", ".join(p.env for p in pollers))
    ThreadingHTTPServer(("0.0.0.0", port), MetricsHandler).serve_forever()


if __name__ == "__main__":
    main()
