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
