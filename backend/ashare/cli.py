from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import jobs, storage
from .cloud import pack_checkpoint, sync_reports, unpack_checkpoint
from .core import atomic_json, data_dir, iso


def main():
    parser = argparse.ArgumentParser(description="A 股观察：本地监测与云端研究")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("probe")
    sub.add_parser("sync")
    sub.add_parser("review")
    training = sub.add_parser("train")
    training.add_argument("--force", action="store_true")
    report = sub.add_parser("report")
    report.add_argument("--mode", choices=["initial", "update", "fallback", "close", "local"], default="local")
    report.add_argument("--no-warm", action="store_true")
    pack = sub.add_parser("pack")
    pack.add_argument("path", type=Path)
    unpack = sub.add_parser("unpack")
    unpack.add_argument("path", type=Path)
    research = sub.add_parser("backtest")
    research.add_argument("code")
    research.add_argument("--strategy", choices=["pullback", "breakout"], default="breakout")
    research.add_argument("--cost-multiplier", type=float, default=1)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        uvicorn.run("ashare.api:app", host="127.0.0.1", port=8765, log_level="info")
    elif args.command == "probe":
        print(json.dumps(jobs.probe(), ensure_ascii=False, indent=2))
    elif args.command == "sync":
        print(json.dumps(sync_reports(), ensure_ascii=False, indent=2))
    elif args.command == "review":
        from .reviews import rebuild
        print(json.dumps(rebuild(),ensure_ascii=False,indent=2))
    elif args.command == "train":
        from .learning import train
        print(json.dumps(train(force=args.force),ensure_ascii=False,indent=2))
    elif args.command == "report":
        result = jobs.build_report(args.mode, warm=not args.no_warm)
        print(json.dumps({k: result.get(k) for k in ["id", "status", "state", "coverage", "error"]}, ensure_ascii=False))
    elif args.command == "pack":
        pack_checkpoint(args.path)
    elif args.command == "unpack":
        unpack_checkpoint(args.path)
    elif args.command == "backtest":
        from .research import daily_baseline
        result = daily_baseline(storage.bars(args.code), args.strategy, args.cost_multiplier)
        path = data_dir() / "research" / f"{args.code}-{args.strategy}.json"
        atomic_json(path, result)
        print(json.dumps({k: v for k, v in result.items() if k not in ["trades", "curve"]}, ensure_ascii=False))
        print(path)


if __name__ == "__main__":
    main()
