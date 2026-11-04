# grafana-dashboards

This is a set of Grafana dashboards that allow users of MetricsHub products to visualize the collected data.

Watch the [MetricsHub labs](https://m8b-demo.metricshub.com/) running Grafana with live data.

# Prerequisites

## Grafana version

**Minimum: Grafana 13.0.1**
These dashboard uses the v2 dashboard schema (Dynamic Dashboards), which is enabled by default in Grafana 13. No configuration required.

## Data source

These dashboard expects a **Prometheus** data source scraping metrics exposed by [MetricsHub](https://metricshub.com/). The data source is selectable at import time.

The OpenTelemetry Collector exporter (`prometheus` or `prometheusremotewrite`) must keep its default metric naming (`.` replaced by `_`, unit suffixes such as `_bytes` or `_seconds`) and export the resource attributes as labels (`resource_to_telemetry_conversion: enabled: true`, as in the MetricsHub OpenTelemetry Collector configuration).

# Red Hat OpenShift and OKD (MetricsHub)

This dashboard shows the Red Hat OpenShift and OKD clusters monitored by the MetricsHub Enterprise **OpenShift** connector.

- The **Cluster** variable lists the `host.name` of the resources monitored by the OpenShift connector.
- The **OpenShift console** links (dashboard link and table links) open `https://console-openshift-console.apps.<cluster domain>`, where `<cluster domain>` is the `host.name` of the resource without its `api.` prefix. This is the default address of the web console: the `console` route of the `openshift-console` namespace, under the default ingress domain `apps.<cluster domain>`. The links require:
  - the resource `host.name` to be the API server host (`api.<cluster domain>`), as in the connector documentation,
  - the default ingress domain and console route (not customized).

  Otherwise, edit the hidden `console_domain` variable of the dashboard.

# Tools

## PromQL indentation

`tools/promql_indent.py` indents the PromQL queries of the dashboards (v2 schema) so that they read well in the query editor. A sub-expression that fits in 100 columns stays on one line; otherwise it is split with a 2-space indentation. Only whitespace changes: a query is rewritten only when its tokens are unchanged, and the queries with comments are kept as written.

It needs Python 3 and no other dependency:

```bash
# List the queries to indent (exit status 1 when there are some)
python tools/promql_indent.py src/dashboards/*/*.json

# Indent them
python tools/promql_indent.py --write src/dashboards/*/*.json

# Also check that Prometheus parses every indented query as the original one
python tools/promql_indent.py --write --prometheus http://localhost:9090 src/dashboards/*/*.json

# Checks of the tool
python tools/test_promql_indent.py
```

# License

The license is GNU Affero General Public License v3.0.
