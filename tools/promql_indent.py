#!/usr/bin/env python3
"""Indents the PromQL queries of Grafana dashboards (v2 schema) so that they read well in the query editor.

A sub-expression that fits in 100 columns stays on one line. Otherwise it is split with a 2-space indentation:
binary expressions at their lowest-precedence operator (a short right operand stays on the operator line), function
calls and aggregations one argument per line, parenthesized expressions in their own block. Selectors and strings are
never split.

Only whitespace changes. An expression is rewritten only when its tokens are unchanged and the result is stable
(indenting it again gives the same text). With --prometheus, it must also keep the same canonical form as returned by
the Prometheus formatter (/api/v1/format_query), which compares the parsed expressions.

Usage:
  python tools/promql_indent.py src/dashboards/*/*.json             report; exit status 1 when a query is not indented
  python tools/promql_indent.py --write src/dashboards/*/*.json     rewrite the files
  python tools/promql_indent.py --write --prometheus http://localhost:9090 <files>
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

WIDTH = 100
STEP = 2
TOKEN = re.compile(r'''
    (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)
  | (?P<comment>\#[^\n]*)
  | (?P<op>==|!=|>=|<=|=~|!~|[-+*/%^<>=])
  | (?P<punct>[(){}\[\],@:])
  | (?P<num>\d[\w.]*)
  | (?P<id>\$\{?\w+\}?|[A-Za-z_][\w:]*)
  | (?P<ws>\s+)
''', re.X)
PRECEDENCE = {'or': 1, 'and': 2, 'unless': 2, '==': 3, '!=': 3, '>=': 3, '<=': 3, '>': 3, '<': 3,
              '+': 4, '-': 4, '*': 5, '/': 5, '%': 5, '^': 6}
MODIFIERS = ('bool', 'by', 'without', 'on', 'ignoring', 'group_left', 'group_right', 'offset')
OPEN, CLOSE = '([{', ')]}'
# Grafana variables that the PromQL parser cannot read outside strings, swapped for literals for --prometheus
SENTINELS = {'${__range}': '7777h', '$__range': '7777h', '${__interval}': '5555s', '$__interval': '5555s',
             '${__rate_interval}': '3333s', '$__rate_interval': '3333s', '${__range_s}': '77771', '$__range_s': '77771',
             '${__range_ms}': '77772', '$__range_ms': '77772', '${__interval_ms}': '55551', '$__interval_ms': '55551'}


class Tok:
    def __init__(self, kind, text, start, end):
        self.kind, self.text, self.start, self.end = kind, text, start, end


def tokenize(s):
    toks, pos = [], 0
    while pos < len(s):
        m = TOKEN.match(s, pos)
        if not m:
            raise ValueError('cannot tokenize at %d: %r' % (pos, s[pos:pos + 30]))
        if m.lastgroup != 'ws':
            toks.append(Tok(m.lastgroup, m.group(0), m.start(), m.end()))
        pos = m.end()
    return toks


def tokens(expr):
    return [(t.kind, t.text) for t in tokenize(expr)]


def one_line(s):
    """The expression on one line: whitespace between tokens collapsed to one space, strings untouched."""
    out, last = [], 0
    for t in tokenize(s):
        if out:
            out.append(' ' if s[last:t.start] else '')
        out.append(t.text)
        last = t.end
    return ''.join(out)


class Formatter:
    def __init__(self, s):
        self.s = s
        self.t = tokenize(s)
        self.match = {}
        stack = []
        for i, t in enumerate(self.t):
            if t.kind == 'punct' and t.text in OPEN:
                stack.append(i)
            elif t.kind == 'punct' and t.text in CLOSE:
                if not stack:
                    raise ValueError('unbalanced %r' % t.text)
                self.match[stack.pop()] = i
        if stack:
            raise ValueError('unbalanced brackets')

    def text(self, i, j):
        return self.s[self.t[i].start:self.t[j - 1].end] if j > i else ''

    def operand_end(self, k):
        t = self.t[k]
        return (t.kind in ('id', 'num', 'str') and t.text not in PRECEDENCE and t.text not in MODIFIERS
                or t.text in CLOSE)

    def ops(self, i, j):
        """Top-level binary operators of tokens [i, j): (start, end, operator), [start, end) being the operator
        with its modifiers (bool, on/ignoring, group_left/group_right)."""
        res, k = [], i
        while k < j:
            t = self.t[k]
            if t.kind == 'punct' and t.text in OPEN:
                k = self.match[k] + 1
                continue
            is_op = (t.kind == 'id' and t.text in ('and', 'or', 'unless')) or (t.kind == 'op' and t.text in PRECEDENCE)
            if is_op and k > i and self.operand_end(k - 1):
                e = k + 1
                if e < j and self.t[e].text == 'bool':
                    e += 1
                if e < j and self.t[e].text in ('on', 'ignoring'):
                    e = self.match[e + 1] + 1
                if e < j and self.t[e].text in ('group_left', 'group_right'):
                    e += 1
                    if e < j and self.t[e].text == '(':
                        e = self.match[e] + 1
                res.append((k, e, t.text))
                k = e
                continue
            k += 1
        return res

    def depth_zero(self, a, n):
        k = a
        while k < n:
            if self.t[k].kind == 'punct' and self.t[k].text in OPEN:
                k = self.match[k] + 1
                if k > n:
                    return False
                continue
            k += 1
        return True

    def suffix(self, i, j):
        s = self.text(i, j)
        return (' ' if s and s[0] != '[' else '') + s

    def fmt(self, i, j, ind):
        """Lines (with their indentation) of the expression made of tokens [i, j)."""
        pad = ' ' * ind
        flat = self.text(i, j)
        if ind + len(flat) <= WIDTH:
            return [pad + flat]
        t = self.t
        ops = self.ops(i, j)
        if ops:
            low = min(PRECEDENCE[o[2]] for o in ops)
            cuts = [o for o in ops if PRECEDENCE[o[2]] == low]
            lines = self.fmt(i, cuts[0][0], ind)
            for n, (a, b, _) in enumerate(cuts):
                end = cuts[n + 1][0] if n + 1 < len(cuts) else j
                op = self.text(a, b)
                rhs = self.fmt(b, end, ind)
                short = len(op) + len(rhs[0]) - ind <= 24
                if len(rhs) == 1 and (short or len(lines[-1]) + len(op) + len(rhs[0]) - ind + 2 <= WIDTH):
                    lines[-1] += ' ' + op + ' ' + rhs[0].lstrip()
                else:
                    lines.append(pad + op)
                    lines += rhs
            return lines
        if t[i].kind == 'punct' and t[i].text == '(':
            k = self.match[i]
            return [pad + '('] + self.fmt(i + 1, k, ind + STEP) + [pad + ')' + self.suffix(k + 1, j)]
        if t[i].kind == 'id' and i + 1 < j:
            p = i + 1
            if t[p].text in ('by', 'without') and p + 1 < j and t[p + 1].text == '(':
                p = self.match[p + 1] + 1
            if p < j and t[p].text == '(':
                k = self.match[p]
                args, a = [], p + 1
                for n in range(p + 1, k + 1):
                    if n == k or (t[n].text == ',' and self.depth_zero(p + 1, n)):
                        if n > a:
                            args.append((a, n))
                        a = n + 1
                body = []  # (lines, literal) per argument; consecutive literal arguments share a line
                for n, (a, b) in enumerate(args):
                    al = self.fmt(a, b, ind + STEP)
                    if n < len(args) - 1:
                        al[-1] += ','
                    literal = all(t[x].kind in ('str', 'num') for x in range(a, b))
                    if literal and body and body[-1][1] and (
                            len(body[-1][0][-1]) + 1 + len(al[0].lstrip()) <= WIDTH or len(al[0].strip()) <= 6):
                        body[-1][0][-1] += ' ' + al[0].lstrip()
                    else:
                        body.append((al, literal))
                lines = [pad + self.text(i, p + 1)]
                for al, _ in body:
                    lines += al
                return lines + [pad + ')' + self.suffix(k + 1, j)]
        return [pad + flat]


def indent(expr):
    s = one_line(expr)
    f = Formatter(s)
    return '\n'.join(line.rstrip() for line in f.fmt(0, len(f.t), 0))


def canonical(expr, prometheus):
    out, last = [], 0
    for t in tokenize(expr):
        out.append(expr[last:t.start])
        out.append(SENTINELS.get(t.text, t.text) if t.kind == 'id' else t.text)
        last = t.end
    url = prometheus.rstrip('/') + '/api/v1/format_query?' + urllib.parse.urlencode({'query': ''.join(out)})
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)['data']


def checked(old, prometheus):
    """(indented expression, None), or (None, reason) when the expression is left as written: it has comments
    (laid out by its author), or it cannot be proven to differ from the indented one by whitespace only."""
    try:
        if any(kind == 'comment' for kind, _ in tokens(old)):
            return None, 'comments'
        new = indent(old)
        if tokens(new) != tokens(old) or indent(new) != new:
            return None, 'unproven'
        if prometheus:
            try:
                before = canonical(old, prometheus)
            except urllib.error.HTTPError:
                before = None  # Prometheus cannot parse it (other Grafana variables): the token check stands
            if before is not None and canonical(new, prometheus) != before:
                return None, 'unproven'
        return new, None
    except (ValueError, KeyError, OSError):
        return None, 'unproven'


def queries(dashboard):
    """(panel, refId, query spec) of the Prometheus queries of a v2 dashboard."""
    for name, el in dashboard['spec']['elements'].items():
        for q in el['spec'].get('data', {}).get('spec', {}).get('queries', []):
            query = q['spec']['query']
            expr = query['spec'].get('expr')
            if query.get('group') == 'prometheus' and isinstance(expr, str) and expr.strip():
                yield name, q['spec'].get('refId'), query['spec']


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('files', nargs='+')
    p.add_argument('--write', action='store_true', help='rewrite the files')
    p.add_argument('--prometheus', help='Prometheus URL: also check the canonical form of every expression')
    a = p.parse_args()
    status = 0
    for path in a.files:
        with open(path, encoding='utf-8') as f:
            raw = f.read()
        d = json.loads(raw)
        if 'elements' not in d.get('spec', {}):
            print('%s: skipped (not a v2 dashboard)' % path)
            continue
        total, todo, left = 0, 0, {'comments': [], 'unproven': []}
        for name, ref, spec in queries(d):
            total += 1
            new, reason = checked(spec['expr'], a.prometheus)
            if reason:
                left[reason].append('%s %s' % (name, ref))
            elif new != spec['expr']:
                todo += 1
                spec['expr'] = new
        refused = left['unproven']
        print('%s: %d queries, %d %s%s%s' % (
            path, total, todo, 'indented' if a.write else 'to indent',
            ', %d kept as written (comments)' % len(left['comments']) if left['comments'] else '',
            ', left as is (not proven whitespace-only): ' + ', '.join(refused) if refused else ''))
        if a.write and todo:
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(json.dumps(d, indent=2, ensure_ascii=False) + ('\n' if raw.endswith('\n') else ''))
        if refused or (todo and not a.write):
            status = 1
    return status


if __name__ == '__main__':
    sys.exit(main())
