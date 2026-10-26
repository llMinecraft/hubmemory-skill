#!/usr/bin/env python3
"""Compare cached vs scan retrieval with identical queries; source files read-only.

The index cache may be created/refreshed under state/. Output omits private text.
Three historical queries are regression examples, not a general recall benchmark.
"""
import argparse
import json
import os
import time
from pathlib import Path

from hybrid_query import search

QUERIES = ('你看一下刚才写的项目立项书', '刚才的立项书', '项目立项书')


def signature(hits):
    return [(h.get('_source'), h.get('_path'), h.get('_source_line'),
             h.get('event_id'), h.get('session_id'), h.get('_score')) for h in hits]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home',default=os.path.expanduser('~/hubmemory'))
    p.add_argument('--cwd',default='/Users/chakeyi/space/yuzu')
    p.add_argument('--as-of',default='2026-10-18T19:01:39+08:00')
    args=p.parse_args()
    rows=[]
    for q in QUERIES:
        results={};timings={}
        for name,enabled in [('scan',False),('indexed',True),('warm',True)]:
            start=time.perf_counter()
            results[name]=search(Path(args.home),q,cwd=args.cwd,as_of=args.as_of,
                                 limit=8,use_index=enabled)
            timings[name]=round(time.perf_counter()-start,4)
        same=signature(results['scan'])==signature(results['indexed'])==signature(results['warm'])
        rows.append({'query':q,'seconds':timings,'exact_top8_scores_and_refs_equal':same,
                     'warm_stats':(results['warm'][0].get('_retrieval') or {}).get('index')
                     if results['warm'] else None})
    print(json.dumps({'source_mutations':False,'cache_writes':True,'cases':rows,
                      'passed':all(r['exact_top8_scores_and_refs_equal'] for r in rows)},
                     ensure_ascii=False,indent=2))
    return 0 if all(r['exact_top8_scores_and_refs_equal'] for r in rows) else 1

if __name__=='__main__':raise SystemExit(main())
