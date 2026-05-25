"""Persistent local cache for MT5 historical bars.

Why this exists
---------------
Repeated backtesting (single window, walk-forward, parameter sweeps) keeps
re-downloading the same bars over and over. That's slow and wastes broker
quota. This module stores per-symbol bars locally and incrementally appends
only newer data on subsequent runs.

Design
------
* One file per symbol, always at M1 base timeframe:
      data_cache/USDJPY_M1.pkl
* Higher timeframes (M5/M15/M30/H1/H4/D1) are aggregated from M1 on demand.
* Storage = pandas pickle (no extra deps, preserves all types, fast).
* If last cached bar is > CACHE_STALE_DAYS old, refresh runs automatically.
* Incremental refresh fetches only (last_cached_time → now) plus a buffer.

Programmatic use
----------------
    from data_cache import DataCache
    cache = DataCache(connector)
    df = cache.get_rates("USDJPY", "M15", n_bars=10_000)

CLI use
-------
    python data_cache.py --info                    # show cache state; creates missing info files
    python data_cache.py --download-live           # download all live SYMBOLS from .env
    python data_cache.py --all-configured          # same as --download-live
    python data_cache.py --symbol USDJPY           # refresh one symbol
    python data_cache.py --symbols USDJPY,USDCHF   # refresh several
    python data_cache.py --symbol USDJPY --bars 1000000 --force   # full re-pull

Per-symbol info files
---------------------
After every download/refresh, a plain-text summary is written alongside the
.pkl file:
    data_cache/USDJPY_M1_info.txt

It contains the date range, number of M1 bars, calendar days covered,
completeness estimate, file size, and freshness status — all derived by
actually reading the .pkl. Running --info also creates any missing info files
retroactively for symbols that were cached before this feature was added.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

from config import settings
from logger import get_logger

log = get_logger("cache")

# pandas resample rules for each MT5 timeframe label
_RESAMPLE_RULES = {
    "M1": "1min",  "M5": "5min",  "M15": "15min", "M30": "30min",
    "H1": "1h",    "H4": "4h",    "D1": "1D",     "W1": "1W",
}


def aggregate_m1(df_m1: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample M1 OHLCV up to a higher timeframe."""
    tf = timeframe.upper()
    if tf == "M1":
        return df_m1.copy()
    rule = _RESAMPLE_RULES.get(tf)
    if rule is None:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "tick_volume" in df_m1.columns:
        agg["tick_volume"] = "sum"
    if "real_volume" in df_m1.columns:
        agg["real_volume"] = "sum"
    if "spread" in df_m1.columns:
        agg["spread"] = "mean"
    out = df_m1.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])


class DataCache:
    """Per-symbol M1 OHLCV cache with incremental refresh + aggregation."""

    def __init__(
        self,
        connector=None,
        cache_dir: Optional[str] = None,
        stale_days: Optional[int] = None,
    ) -> None:
        self.connector = connector
        self.cache_dir = Path(cache_dir or settings.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stale_days = stale_days if stale_days is not None else settings.cache_stale_days

    # ─── file paths ────────────────────────────────────────────────────── #

    def path_for(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}_M1.pkl"

    # ─── load / save ───────────────────────────────────────────────────── #

    def load(self, symbol: str) -> Optional[pd.DataFrame]:
        p = self.path_for(symbol)
        if not p.exists():
            return None
        try:
            df = pd.read_pickle(p)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            return df
        except Exception as exc:
            log.warning("[%s] cache read failed: %s", symbol, exc)
            return None

    def info_path_for(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}_M1_info.txt"

    def save(self, symbol: str, df: pd.DataFrame) -> None:
        try:
            df.to_pickle(self.path_for(symbol))
        except Exception as exc:
            log.warning("[%s] cache write failed: %s", symbol, exc)

    def write_info_file(self, symbol: str, df: Optional[pd.DataFrame] = None) -> None:
        """Write a human-readable info file for a symbol's cache."""
        if df is None:
            df = self.load(symbol)
        pkl_path = self.path_for(symbol)
        info_path = self.info_path_for(symbol)

        if df is None or df.empty:
            info_path.write_text(
                f"Symbol: {symbol}\n"
                f"Status: NO DATA (cache file missing or empty)\n"
                f"File:   {pkl_path}\n",
                encoding="utf-8",
            )
            return

        span_seconds = (df.index[-1] - df.index[0]).total_seconds()
        span_days = span_seconds / 86400
        span_trading_days = span_seconds / (5 / 7 * 86400)  # approx forex trading days
        age = datetime.now(timezone.utc) - df.index[-1]
        age_str = str(age).split(".")[0]
        stale = age > timedelta(days=self.stale_days)
        size_mb = pkl_path.stat().st_size / (1024 * 1024) if pkl_path.exists() else 0.0

        # Estimate actual trading bars (M1 bars per forex day ≈ 1440 * 5/7 ≈ 1029)
        calendar_days_expected = span_days * (1440 * 5 / 7)
        completeness_pct = (len(df) / calendar_days_expected * 100) if calendar_days_expected > 0 else 0.0

        lines = [
            f"Symbol         : {symbol}",
            f"File           : {pkl_path}",
            f"",
            f"Date range     : {df.index[0].strftime('%Y-%m-%d %H:%M UTC')}",
            f"               → {df.index[-1].strftime('%Y-%m-%d %H:%M UTC')}",
            f"Calendar days  : {span_days:.1f}",
            f"Approx trading days: {span_days * 5 / 7:.1f}  (excl. weekends)",
            f"",
            f"M1 bars stored : {len(df):,}",
            f"Completeness   : {min(completeness_pct, 100.0):.0f}%  (vs expected M1 bars)",
            f"File size      : {size_mb:.2f} MB",
            f"",
            f"Last bar       : {df.index[-1].strftime('%Y-%m-%d %H:%M UTC')}",
            f"Age            : {age_str}",
            f"Status         : {'STALE (needs refresh)' if stale else 'FRESH'}",
            f"Stale after    : {self.stale_days} days without update",
            f"",
            f"Columns        : {', '.join(df.columns.tolist())}",
            f"",
            f"Generated      : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        ]
        info_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.debug("[%s] info file written → %s", symbol, info_path)

    # ─── freshness ─────────────────────────────────────────────────────── #

    def is_stale(self, df: Optional[pd.DataFrame]) -> bool:
        if df is None or df.empty:
            return True
        age = datetime.now(timezone.utc) - df.index[-1]
        return age > timedelta(days=self.stale_days)

    # ─── refresh ───────────────────────────────────────────────────────── #

    def refresh(
        self,
        symbol: str,
        max_bars: Optional[int] = None,
        force_initial: bool = False,
    ) -> Optional[pd.DataFrame]:
        """Pull newer M1 bars from MT5 and merge into the cache.

        * If no cache exists (or `force_initial`), fetches up to `max_bars`
          (default `CACHE_INITIAL_BARS`) — most recent bars first.
        * Else fetches just enough to cover (last_cached_time → now) + buffer.
        """
        if self.connector is None:
            raise RuntimeError("DataCache.refresh requires an MT5 connector")

        cached = None if force_initial else self.load(symbol)

        if cached is None or cached.empty:
            n_fetch = max_bars or settings.cache_initial_bars
            log.info("[%s] no cache — fetching %d M1 bars from MT5", symbol, n_fetch)
        else:
            last_time = cached.index[-1]
            now = datetime.now(timezone.utc)
            gap_min = max(60, int((now - last_time).total_seconds() / 60) + 60)
            n_fetch = min(max_bars or settings.cache_initial_bars, gap_min)
            if n_fetch < 30:
                log.debug("[%s] cache fresh (last bar %s)", symbol, last_time)
                return cached
            log.info("[%s] cache last=%s — fetching %d new M1 bars",
                     symbol, last_time.strftime("%Y-%m-%d %H:%M"), n_fetch)

        new_df = self.connector.get_rates(symbol, "M1", n_bars=n_fetch)
        if new_df is None or new_df.empty:
            log.warning("[%s] MT5 returned no data", symbol)
            return cached

        if cached is None or cached.empty:
            merged = new_df
        else:
            merged = pd.concat([cached, new_df])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()

        self.save(symbol, merged)
        span_days = (merged.index[-1] - merged.index[0]).total_seconds() / 86400
        log.info("[%s] cache now %d M1 bars (%.1f days: %s → %s)",
                 symbol, len(merged), span_days,
                 merged.index[0].strftime("%Y-%m-%d"),
                 merged.index[-1].strftime("%Y-%m-%d"))
        self.write_info_file(symbol, merged)
        return merged

    # ─── public read API ───────────────────────────────────────────────── #

    def get_rates(
        self,
        symbol: str,
        timeframe: str,
        n_bars: int = 500,
        allow_refresh: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Return `n_bars` of `timeframe`, aggregated from cached M1.

        Auto-refreshes if the cache is stale (or missing) and `allow_refresh`
        is True. If `allow_refresh=False`, returns whatever's on disk (or
        None if nothing is cached).
        """
        df = self.load(symbol)
        if (df is None or self.is_stale(df)) and allow_refresh:
            if self.connector is None:
                log.warning("[%s] cache stale/missing and no connector to refresh", symbol)
            else:
                df = self.refresh(symbol)
        if df is None or df.empty:
            return None

        # Need enough M1 bars to satisfy n_bars at the requested timeframe.
        # Aggregate, then return the most recent n_bars.
        out = aggregate_m1(df, timeframe)
        if n_bars and len(out) > n_bars:
            out = out.iloc[-n_bars:]
        return out


# ════════════════════════════════════════════════════════════════════════ #
# CLI                                                                       #
# ════════════════════════════════════════════════════════════════════════ #

def _show_info(cache: DataCache) -> None:
    files = sorted(cache.cache_dir.glob("*_M1.pkl"))
    if not files:
        print(f"Cache empty — {cache.cache_dir}")
        return
    print(f"Cache directory : {cache.cache_dir.resolve()}")
    print(f"Symbols cached  : {len(files)}")
    print(f"Stale threshold : {cache.stale_days} days")
    print()
    for fp in files:
        sym = fp.stem.replace("_M1", "")
        df = cache.load(sym)
        # Always ensure info file exists (create if missing)
        info_path = cache.info_path_for(sym)
        if not info_path.exists():
            cache.write_info_file(sym, df)
        if df is None or df.empty:
            print(f"  {sym:<12} : (empty)")
            continue
        span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400
        age = datetime.now(timezone.utc) - df.index[-1]
        stale_tag = "STALE" if age > timedelta(days=cache.stale_days) else "fresh"
        size_mb = fp.stat().st_size / (1024 * 1024)
        print(
            f"  {sym:<12} : {len(df):>8,} M1 bars  {span_days:>6.1f} days  "
            f"last={df.index[-1].strftime('%Y-%m-%d %H:%M')} "
            f"({str(age).split('.')[0]} ago)  [{stale_tag}, {size_mb:.1f} MB]  "
            f"→ {info_path.name}"
        )
    print()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Download / refresh local M1 historical data cache",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python data_cache.py --info                      # show cache state + create missing info files
  python data_cache.py --download-live             # download all live SYMBOLS from .env
  python data_cache.py --all-configured            # same as --download-live
  python data_cache.py --symbol USDJPY             # refresh one symbol
  python data_cache.py --symbols EURUSD,GBPUSD     # refresh several
  python data_cache.py --symbol USDJPY --force     # full re-download from scratch
  python data_cache.py --bars 500000               # override bars limit
""",
    )
    p.add_argument("--symbol", help="Refresh a single symbol")
    p.add_argument("--symbols", help="Comma-separated symbol list to refresh")
    p.add_argument("--all-configured", action="store_true",
                   help="Refresh every symbol in SYMBOLS from .env (same as --download-live)")
    p.add_argument("--download-live", action="store_true",
                   help="Download all live SYMBOLS from .env using CACHE_INITIAL_BARS")
    p.add_argument("--bars", type=int, default=settings.cache_initial_bars,
                   help=f"Max M1 bars to fetch (default {settings.cache_initial_bars:,})")
    p.add_argument("--force", action="store_true",
                   help="Re-download from scratch instead of incremental update")
    p.add_argument("--info", action="store_true",
                   help="Show current cache state and exit (no fetch)")
    args = p.parse_args()

    cache = DataCache(connector=None)

    if args.info or not (args.symbol or args.symbols or args.all_configured or args.download_live):
        _show_info(cache)
        return

    if args.symbol:
        targets: List[str] = [args.symbol]
    elif args.symbols:
        targets = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        # --all-configured or --download-live both use settings.symbols
        targets = list(settings.symbols)

    if not targets:
        print("No symbols configured in SYMBOLS (.env). Nothing to download.")
        return

    print(f"\nDownloading {len(targets)} symbol(s): {', '.join(targets)}")
    print(f"Max M1 bars per symbol : {args.bars:,}  (~{args.bars / (1440 * 5/7):.0f} trading days)")
    print(f"Mode                   : {'full re-download' if args.force else 'incremental (only new bars)'}")
    print(f"Output directory       : {cache.cache_dir.resolve()}")
    print()

    from mt5_connector import MT5Connector
    c = MT5Connector()
    if not c.connect():
        print("ERROR: MT5 connect failed")
        sys.exit(1)
    cache.connector = c
    try:
        for i, sym in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {sym} ...", flush=True)
            df = cache.refresh(sym, max_bars=args.bars, force_initial=args.force)
            if df is not None and not df.empty:
                span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400
                size_mb = cache.path_for(sym).stat().st_size / (1024 * 1024)
                print(
                    f"         done: {len(df):,} M1 bars  |  {span_days:.1f} calendar days  "
                    f"|  {size_mb:.2f} MB  |  info → {cache.info_path_for(sym).name}"
                )
            else:
                print(f"         SKIPPED (no data returned)")
    finally:
        c.shutdown()

    print()
    _show_info(DataCache(connector=None))


if __name__ == "__main__":
    main()
