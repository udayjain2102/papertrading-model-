"""CLI for the full out-of-sample gate."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from ..factor.universe import UNIVERSE, load_universe
from ..strategies import REGISTRY
from .gate import run_gate


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="rhagent.gate")
    parser.add_argument("--strategy", required=True, choices=sorted(REGISTRY))
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--min-names", type=int, default=10)
    parser.add_argument("--oos-frac", type=float, default=0.25)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--icir-floor", type=float, default=0.3)
    parser.add_argument("--half-life-floor", type=int, default=5)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--dsr-threshold", type=float, default=0.95)
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--symbols", help="comma-separated override of the default universe")
    parser.add_argument("--cache-dir", default="data")
    args = parser.parse_args(argv)

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else UNIVERSE
    )
    end = date.today()
    start = end - timedelta(days=args.days)
    bars_by_symbol, close = load_universe(
        symbols,
        start.isoformat(),
        end.isoformat(),
        cache_dir=args.cache_dir,
    )
    result = run_gate(
        args.strategy,
        bars_by_symbol,
        close,
        horizon=args.horizon,
        min_names=args.min_names,
        oos_frac=args.oos_frac,
        rounds=args.rounds,
        icir_floor=args.icir_floor,
        half_life_floor=args.half_life_floor,
        alpha=args.alpha,
        dsr_threshold=args.dsr_threshold,
    )

    print(
        f"strategy: {result.strategy}   universe: {len(bars_by_symbol)} names   "
        f"configs tested: {result.n_tested}"
    )
    print(f"corrections: Bonferroni alpha/N, Deflated-Sharpe > {args.dsr_threshold}")
    if not result.rows:
        print("  no survivors from the in-sample search")
    for row in result.rows:
        tag = "VIABLE" if row.viable else row.reason
        print(
            f"  {row.params}  IS_ICIR={row.is_icir:+.3f}  "
            f"OOS_ICIR={row.oos_icir:+.3f}  half_life={row.oos_half_life}  "
            f"bonf_p={row.bonf_p:.2e}<{row.bonf_threshold:.2e}?{row.bonf_pass}  "
            f"DSR={row.dsr:.3f}  -> {tag}"
        )
    print(f"\nviable: {len(result.viable)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
