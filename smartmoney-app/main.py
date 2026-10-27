"""Smart-money tracker for Solana.

  python main.py setup               winners + discover + vet in one go
  python main.py winners             find past winners from price history (auto)
  python main.py discover            find wallets early on those winners
  python main.py vet                 measure candidates, drop bots/clusters, track the best
  python main.py pick                re-apply the vetting rules to stored stats (no credits)
  python main.py add WALLET [label]  track a wallet by hand
  python main.py remove WALLET       stop tracking a wallet
  python main.py wallets             list candidates and tracked wallets
  python main.py track --once        one polling cycle, print signals
  python main.py paper               paper-trading results so far
  python main.py test-alert          send a test Telegram message
  python main.py report [--send]     daily holdings/accumulation report (print, or email it now)
  python main.py backtest            test "copy at a wallet's Nth buy" on stored trades (no credits)
  python main.py scout [--days 30]   find few-trade wallets winning 90%+, and the groups they trade in
  python main.py scout --profile patient   find slow accumulators: few tokens, repeat buys, mostly wins
  python main.py holders [--mints M ...]   find big holders who built positions slowly in coins that ran
  python main.py patternbt           backtest: is accumulation visible before coins run?
  python main.py trace               who profited on the winners, and who funded them
  python main.py review [--days 7]   each tracked wallet's % gains, and coins they shared
  python main.py serve               dashboard + tracker loop (Replit's Run button)
"""
import argparse
import sys

from smartmoney import config, db, discover, tracker, vet, winners


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("setup")
    sub.add_parser("winners")
    sub.add_parser("discover")
    sub.add_parser("vet")
    sub.add_parser("pick")
    a = sub.add_parser("add"); a.add_argument("wallet"); a.add_argument("label", nargs="?")
    r = sub.add_parser("remove"); r.add_argument("wallet")
    sub.add_parser("wallets")
    t = sub.add_parser("track"); t.add_argument("--once", action="store_true")
    sub.add_parser("serve")
    sub.add_parser("paper")
    sub.add_parser("test-alert")
    rp = sub.add_parser("report"); rp.add_argument("--send", action="store_true")
    sub.add_parser("backtest")
    tr = sub.add_parser("trace")
    tr.add_argument("--per-coin", type=int, default=40, help="early buyers checked per winner")
    tr.add_argument("--depth", type=int, default=3, help="funding hops to climb")
    tr.add_argument("--min-profit", type=float, default=5.0, help="SOL profit to count")
    tr.add_argument("--wallets", nargs="*", default=[], help="extra wallets to trace")
    tr.add_argument("--out", default="trace.csv")
    pb = sub.add_parser("patternbt")
    pb.add_argument("--controls", type=int, default=60)
    pb.add_argument("--lo", type=float, default=100_000, help="checkpoint market cap floor")
    pb.add_argument("--hi", type=float, default=1_500_000, help="checkpoint market cap ceiling")
    pb.add_argument("--min-age-days", type=float, default=2)
    pb.add_argument("--window", type=float, default=48, help="hours scored before checkpoint")
    pb.add_argument("--max-txs", type=int, default=10_000)
    pb.add_argument("--out", default="patternbt.csv", help="CSV file to write")
    hd = sub.add_parser("holders")
    hd.add_argument("--mints", nargs="*", default=[], help="extra coins to check")
    hd.add_argument("--top", type=int, default=20, help="largest holders per coin (max 20)")
    hd.add_argument("--min-multiple", type=float, default=2.0)
    sc = sub.add_parser("scout")
    sc.add_argument("--profile", choices=["group", "patient"], default="group")
    sc.add_argument("--days", type=float, default=None)
    sc.add_argument("--top", type=int, default=3000)
    sc.add_argument("--min-trades", type=int, default=5)
    sc.add_argument("--max-trades", type=int, default=40)
    sc.add_argument("--min-win", type=float, default=0.9)
    sc.add_argument("--wallets", nargs="*", default=None,
                    help="patient: profile just these wallets instead of the discovery pool")
    sc.add_argument("--max-tx", type=int, default=300,
                    help="patient: most transactions a wallet may make in the window")
    sc.add_argument("--min-hits", type=int, default=2,
                    help="patient: winners a wallet bought early (1 = widest, ~10x the credits)")
    rv = sub.add_parser("review"); rv.add_argument("--days", type=float, default=7)
    args = ap.parse_args(argv)

    if args.cmd == "setup":
        winners.run(); print(); discover.run(); print(); vet.run()
    elif args.cmd == "winners":
        winners.run()
    elif args.cmd == "discover":
        discover.run()
    elif args.cmd == "vet":
        vet.run()
    elif args.cmd == "pick":
        vet.pick()
    elif args.cmd == "add":
        db.upsert_wallet(db.connect(), args.wallet, label=args.label, source="manual",
                         tracked=1)
        print(f"Tracking {args.wallet}")
    elif args.cmd == "remove":
        db.upsert_wallet(db.connect(), args.wallet, tracked=0)
        print(f"Stopped tracking {args.wallet}")
    elif args.cmd == "wallets":
        con = db.connect()
        rows = con.execute("""SELECT * FROM wallets WHERE tracked=1 OR tx_per_day IS NOT NULL
                              ORDER BY tracked DESC, hits DESC""").fetchall()
        for w in rows:
            status = "TRACKED" if w["tracked"] else (w["flag"] or "not picked")
            if w["cluster"] and not w["tracked"]:
                status += " (cluster)"
            print(f"{w['address']}  hits={w['hits'] or 0:<3} {status}  {w['label'] or ''}")
        print(f"\n{len(rows)} vetted or tracked wallets shown.")
    elif args.cmd == "track":
        if args.once:
            from smartmoney.helius import Helius
            tracker.cycle(Helius(), db.connect())
        else:
            tracker.loop()
    elif args.cmd == "review":
        from smartmoney import review
        review.run(days=args.days)
    elif args.cmd == "scout":
        from smartmoney import scout
        if args.profile == "patient":
            scout.run_patient(days=args.days or 60, min_hits=args.min_hits,
                              max_tx=args.max_tx if not args.wallets else max(args.max_tx, 3000),
                              wallets=args.wallets,
                              min_win=args.min_win if args.min_win != 0.9 else 0.8)
        else:
            scout.run(days=args.days or 30, top=args.top, min_trades=args.min_trades,
                      max_trades=args.max_trades, min_win=args.min_win)
    elif args.cmd == "trace":
        from smartmoney import trace
        trace.run(per_coin=args.per_coin, depth=args.depth, min_profit_sol=args.min_profit,
                  extra_wallets=args.wallets, csv_path=args.out)
    elif args.cmd == "patternbt":
        from smartmoney import patternbt
        patternbt.run(controls=args.controls, lo=args.lo, hi=args.hi,
                      min_age_days=args.min_age_days, window_h=args.window,
                      max_txs=args.max_txs, csv_path=args.out)
    elif args.cmd == "holders":
        from smartmoney import holders
        holders.run(extra_mints=args.mints, top=min(args.top, 20),
                    min_multiple=args.min_multiple)
    elif args.cmd == "backtest":
        from smartmoney import backtest
        backtest.run()
    elif args.cmd == "report":
        from smartmoney import report, tracker
        if args.send:
            report.send(notify=tracker.notify)
        else:
            print(report.render(report.gather(db.connect()))[2])
    elif args.cmd == "test-alert":
        import requests
        if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
            print("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Replit Secrets first.")
            return 1
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": config.TELEGRAM_CHAT_ID,
                                "text": "✅ Smart Money Tracker alerts are working."}, timeout=15)
        print("Sent. Check Telegram." if r.ok else f"Telegram said: {r.text}")
        return 0 if r.ok else 1
    elif args.cmd == "paper":
        from smartmoney import paper
        paper.print_summary()
    elif args.cmd == "serve" or args.cmd is None:
        from smartmoney import web
        web.serve(config.PORT)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    from smartmoney.helius import HeliusError
    try:
        sys.exit(main())
    except HeliusError as e:
        print(f"Error: {e}")
        sys.exit(1)
