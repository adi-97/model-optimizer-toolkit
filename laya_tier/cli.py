import argparse
import json
import logging
import re
import sys
import time


def parse_since(value):
    if value in (None, "all"):
        return 0
    match = re.fullmatch(r"(\d+)([mhd])", value)
    if not match:
        raise argparse.ArgumentTypeError("use e.g. 30m, 24h, 7d or all")
    seconds = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return time.time() - seconds


def cmd_serve(args):
    import uvicorn

    from .proxy import create_app, load_config

    config = load_config(args.config, host=args.host, port=args.port, upstream=args.upstream,
                         classifier_model=args.classifier_model, classifier_device=args.device,
                         shadow=args.shadow, db_path=args.db)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning")


def cmd_stats(args):
    from .proxy import load_config
    from .proxy.tracker import Tracker

    config = load_config(args.config, db_path=args.db)
    tracker = Tracker(config.db_path)
    rows = tracker.summary(by=args.by, since=parse_since(args.since))
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    if not rows:
        print("no requests recorded in %s" % config.db_path)
        return
    print("%-44s %8s %7s %12s %12s %10s %10s %10s %10s" % (args.by, "requests", "routed", "prompt_tok", "cache_read",
                                                          "output_tok", "cost_$", "unrouted_$", "saved_$"))
    total = {k: 0 for k in rows[0] if k != "group"}
    for r in rows:
        for k in total:
            total[k] += r[k] or 0
        print_row(r)
    print_row(dict(total, group="TOTAL"))
    if total["counterfactual_usd"]:
        print("estimated saving: %.1f%%  (unrouted_$ prices the same tokens at the model the agent asked for)"
              % (100 * (total["counterfactual_usd"] - total["cost_usd"]) / total["counterfactual_usd"]))


def print_row(r):
    print("%-44s %8d %7d %12d %12d %10d %10.4f %10.4f %10.4f" % (
        str(r["group"])[:44], r["requests"], r["routed"] or 0, r["prompt_tokens"], r["cache_read_tokens"],
        r["output_tokens"], r["cost_usd"], r["counterfactual_usd"], r["counterfactual_usd"] - r["cost_usd"]))


def cmd_classify(args):
    from .proxy import load_config
    from .proxy.policy import Router
    from .proxy.pricing import Pricing
    from . import TierClassifier

    config = load_config(args.config, classifier_model=args.classifier_model, classifier_device=args.device)
    classifier = TierClassifier.load(config.classifier_model, device=config.classifier_device,
                                     min_confidence=config.min_confidence)
    router = Router(config, classifier, Pricing(config.prices))
    for text in args.text:
        body = {"model": args.model, "messages": [{"role": "user", "content": text}]}
        d = router.decide(body)
        print("%-7s %.3f  %s -> %s%s  %s" % (d.tier, d.confidence, d.requested_model, d.target_model,
                                            " effort=%s" % d.effort if d.effort else "", text[:80]))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="laya-tier")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the routing proxy")
    serve.add_argument("--config")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--upstream")
    serve.add_argument("--classifier-model")
    serve.add_argument("--device")
    serve.add_argument("--db")
    serve.add_argument("--shadow", action="store_const", const=True)
    serve.add_argument("-v", "--verbose", action="store_true")
    serve.set_defaults(func=cmd_serve)

    stats = sub.add_parser("stats", help="summarize tracked usage")
    stats.add_argument("--config")
    stats.add_argument("--db")
    stats.add_argument("--since", default="24h")
    stats.add_argument("--by", default="tier", choices=["tier", "model", "route", "source", "role", "session", "day"])
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    classify = sub.add_parser("classify", help="show the tier and route for prompts")
    classify.add_argument("text", nargs="+")
    classify.add_argument("--config")
    classify.add_argument("--model", default="claude-opus-5-5")
    classify.add_argument("--classifier-model")
    classify.add_argument("--device")
    classify.set_defaults(func=cmd_classify)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
