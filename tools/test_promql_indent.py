# Checks of promql_indent. Run: python tools/test_promql_indent.py
from promql_indent import checked, indent, tokens

LONG = ('(count(k8s_pod_status_phase{host_name="$cluster", k8s_namespace_name=~"$namespace", state="Running"} == 1 '
        'and (timestamp(k8s_pod_status_phase{host_name="$cluster", k8s_node_name=~"$node"}) >= on (host_name) '
        'group_left max by (host_name) (timestamp(k8s_pod_status_phase{host_name="$cluster"})) - 30)) or vector(0)) '
        'and on () (count(k8s_pod_status_phase{host_name="$cluster"}) > 0)')
LONG_INDENTED = '''\
(
  count(
    k8s_pod_status_phase{host_name="$cluster", k8s_namespace_name=~"$namespace", state="Running"} == 1
    and
    (
      timestamp(k8s_pod_status_phase{host_name="$cluster", k8s_node_name=~"$node"})
      >= on (host_name) group_left
      max by (host_name) (timestamp(k8s_pod_status_phase{host_name="$cluster"})) - 30
    )
  ) or vector(0)
) and on () (count(k8s_pod_status_phase{host_name="$cluster"}) > 0)'''
GRAFANA_VARS = ('max_over_time((count(max by (host_name, id) (openshift_operator_status{host_name="$cluster", '
                'state!="ok"} == 1)) or vector(0))[$__interval:1m]) and on () (count(k8s_namespace_phase'
                '{host_name="$cluster"} offset $__interval) > 0)')

# short expressions stay as they are
assert indent('sum by (job) (rate(http_requests_total{job="api"}[5m]))') == 'sum by (job) (rate(http_requests_total{job="api"}[5m]))'
# long expressions are split, short right operands stay on the operator line
assert indent(LONG) == LONG_INDENTED, indent(LONG)
# stable, and whitespace is the only change
assert indent(LONG_INDENTED) == LONG_INDENTED
assert tokens(LONG_INDENTED) == tokens(LONG)
# any existing layout gives the same result
assert indent(LONG_INDENTED.replace('\n', '\n\n   ')) == LONG_INDENTED
# Grafana variables in subqueries, offsets and strings are kept
out, reason = checked(GRAFANA_VARS, None)
assert out and not reason and '[$__interval:1m]' in out and 'offset $__interval' in out and '"$cluster"' in out, out
# commented expressions are kept as written, what cannot be tokenized is left as is
assert checked('# total\nsum(up)', None) == (None, 'comments')
assert checked('{job="a"} |= "error"', None) == (None, 'unproven')
print('ok')
