#!/usr/bin/env python3
"""Evaluate search/query against manually verified conversation ids."""
import argparse, json, time
from pathlib import Path
from typer.testing import CliRunner
from ai_convos.cli import app

def main():
    p = argparse.ArgumentParser(); p.add_argument("data", type=Path); p.add_argument("--method", choices=("query", "search"), default="query"); p.add_argument("-k", type=int, default=8); a = p.parse_args()
    cases = [json.loads(x) for x in a.data.read_text().splitlines() if x.strip()]; rows, runner = [], CliRunner()
    for c in cases:
        t = time.perf_counter(); r = runner.invoke(app, [a.method, c["query"], "-n", str(a.k), "-f", "json"]); elapsed = time.perf_counter()-t
        try: hits = json.loads(next(x for x in r.output.splitlines() if x.startswith("[")))
        except Exception: hits = []
        ids = [x["conversation_id"] for x in hits]; rank = next((i+1 for i, x in enumerate(ids) if x in c["expected"]), None)
        rows.append(dict(query=c["query"], rank=rank, top=ids[:3], seconds=round(elapsed, 2), duplicates=len(ids)-len(set(ids)), error=None if r.exit_code == 0 and hits else (r.output+r.stderr).strip()[-300:]))
        print(json.dumps(rows[-1]))
    ok = [r for r in rows if r["rank"]]; print(json.dumps(dict(method=a.method, cases=len(rows), recall_at_k=len(ok)/len(rows), mrr=sum(1/r["rank"] for r in ok)/len(rows), mean_seconds=sum(r["seconds"] for r in rows)/len(rows), duplicates=sum(r["duplicates"] for r in rows))))

if __name__ == "__main__": main()
