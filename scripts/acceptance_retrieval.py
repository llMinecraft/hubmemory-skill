#!/usr/bin/env python3
"""Read-only real-history acceptance: no model, no source/data writes.

Use --json for a compact auditable test report. This reconstructs eligible
historical evidence; it is NOT a snapshot of watcher state at that instant.
"""
from __future__ import annotations
import argparse
import json
import os
import time
from pathlib import Path

from hybrid_query import search
from context import compact_hit, build_payload, render, estimate_tokens


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', default=os.path.expanduser('~/hubmemory'))
    parser.add_argument('--cwd', default='/Users/chakeyi/space/yuzu')
    parser.add_argument('--as-of', default='2026-10-18T19:01:39+08:00')
    parser.add_argument('--expected-session', default='session-e98a601a-9496-4821-9366-0d55e4a073e6')
    parser.add_argument('--expected-artifact', default='SYNTHESIS_CHAMBER_CHARTER.md')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    checks = []
    for query in ('你看一下刚才写的项目立项书', '刚才的立项书', '项目立项书'):
        start = time.monotonic()
        hits = search(Path(args.home), query, cwd=args.cwd, as_of=args.as_of, limit=8)
        top = compact_hit(hits[0]) if hits else None
        payload = build_payload(query, hits, 800)
        paths = [a.get('path', '') for a in (top or {}).get('artifacts', [])]
        ok = bool(top and top['session'] == args.expected_session and
                  any(p.endswith(args.expected_artifact) for p in paths) and
                  top['event_id'] and top['source_line'] and
                  payload['results'] and payload['sufficiency'] == 'sufficient' and
                  estimate_tokens(render(payload)) <= 800)
        checks.append(dict(query=query, passed=ok, seconds=round(time.monotonic()-start, 3),
                           session=(top or {}).get('session'), event_id=(top or {}).get('event_id'),
                           source=(top or {}).get('locator'), artifacts=paths,
                           delivery_state=(top or {}).get('retrieval', {}).get('delivery_state'),
                           sufficiency=payload['sufficiency'], tokens_est=estimate_tokens(render(payload))))
    report = dict(as_of=args.as_of, cutoff_exclusive=True, cwd=args.cwd,
                  historical_watcher_snapshot=False, tests=checks,
                  passed=all(c['passed'] for c in checks))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print('[%s] %s | %.3fs | %s' % ('PASS' if check['passed'] else 'FAIL', check['query'],
                                             check['seconds'], ', '.join(check['artifacts'])))
        print('Historical evidence reconstruction, not a watcher-state snapshot.')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
