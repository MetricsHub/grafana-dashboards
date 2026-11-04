# Checks of promql_indent. Run: python tools/test_promql_indent.py
import http.server
import json
import threading
import urllib.error

from promql_indent import ParseError, checked, indent, tokens

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

# numbers with an exponent sign or a leading dot are one token, never split at their sign
SELECTOR = 'k8s_pod_memory_working_set_bytes{host_name="$cluster", k8s_namespace_name=~"$namespace", k8s_node_name=~"$node"}'
assert ('num', '1e-3') in tokens(SELECTOR + ' * 1e-3') and ('num', '.5') in tokens('x > .5')
assert '1e-3' in indent('sum(' + SELECTOR + ') * 1e-3 + sum(' + SELECTOR + ') * 2E+6')
# atan2 is a binary operator, at the precedence of *
assert indent(SELECTOR + ' atan2 ' + SELECTOR) == SELECTOR + '\natan2\n' + SELECTOR


# with --prometheus, only a parse error (400 bad_data) falls back to the token check; other endpoint errors propagate
class Prometheus(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = self.server.answer
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(('127.0.0.1', 0), Prometheus)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = 'http://127.0.0.1:%d' % server.server_port
server.answer = (400, {'status': 'error', 'errorType': 'bad_data', 'error': 'parse error'})
assert checked(LONG, url) == (LONG_INDENTED, None)
for code in (401, 404, 500):
    server.answer = (code, {'status': 'error', 'errorType': 'internal'})
    try:
        checked(LONG, url)
        raise AssertionError('HTTP %d must fail the check' % code)
    except urllib.error.HTTPError as e:
        assert e.code == code and not isinstance(e, ParseError)
server.shutdown()
print('ok')
