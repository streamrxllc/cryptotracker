"""Smart-money tracker for Solana.

  python main.py discover            find wallets early on the tokens in winners.txt
  python main.py vet                 drop bots/clusters, start tracking the best
  python main.py add WALLET [label]  track a wallet by hand
  python main.py remove WALLET       stop tracking a wallet
  python main.py wallets             list candidates and tracked wallets
  python main.py track --once        one polling cycle, print signals
  python main.py serve               dashboard + tracker loop (Replit's Run button)
"""
import argparse
import sys

from smartmoney import config, db, discover, tracker, vet


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("discover")
    sub.add_parser("vet")
    a = sub.add_parser("add"); a.add_argument("wallet"); a.add_argument("label", nargs="?")
    r = sub.add_parser("remove"); r.add_argument("wallet")
    sub.add_parser("wallets")
    t = sub.add_parser("track"); t.add_argument("--once", action="store_true")
    sub.add_parser("serve")
    args = ap.parse_args(argv)

    if args.cmd == "discover":
        discover.run()
    elif args.cmd == "vet":
        vet.run()
    elif args.cmd == "add":
        db.upsert_wallet(db.connect(), args.wallet, label=args.label, source="manual",
                         tracked=1)
        print(f"Tracking {args.wallet}")
    elif args.cmd == "remove":
        db.upsert_wallet(db.connect(), args.wallet, tracked=0)
        print(f"Stopped tracking {args.wallet}")
    elif args.cmd == "wallets":
        con = db.connect()
        for w in con.execute("SELECT * FROM wallets ORDER BY tracked DESC, hits DESC"):
            flags = ("TRACKED " if w["tracked"] else "        ") + \
                    ("bot " if w["is_bot"] else "") + ("cluster " if w["cluster"] else "")
            print(f"{flags:<20} {w['address']}  hits={w['hits'] or 0} "
                  f"tx/day={w['tx_per_day'] or 0:.0f} {w['label'] or ''}")
    elif args.cmd == "track":
        if args.once:
            from smartmoney.helius import Helius
            tracker.cycle(Helius(), db.connect())
        else:
            tracker.loop()
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
