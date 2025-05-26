#!/usr/bin/env python3

"""
evt_filter.py  –  Parallel EVTX/EVT time-window extractor …
"""

import argparse
import csv
import datetime as dt
import logging
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple
from io import StringIO
import pandas as pd
import uuid
try:
    from pandas.errors import XPathError
except ImportError:        # pandas < 2.2
    class XPathError(Exception):
        """Dummy shim for old pandas versions."""
        pass

# ──────────────────────────── constants ────────────────────────────
DEFAULT_LOGPARSER_PATH = r"LogParser.exe"
ENC_RE = re.compile(rb'encoding="([^"]+)"', re.I)

# ────────────────────────── argument parsing ───────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Aggregate .evt/.evtx logs into a single CSV (time-window + EventID filters)."
    )
    p.add_argument("--dir", required=True,
                   help="Root folder with .evt/.evtx files (searched recursively).")
    p.add_argument("--output", required=True, help="Destination CSV file.")
    p.add_argument("--start-date", required=True,
                   help="Start datetime 'YYYY-MM-DD HH:MM:SS'")
    p.add_argument("--end-date", required=True,
                   help="End datetime 'YYYY-MM-DD HH:MM:SS'")

    # inclusive / exclusive EventID filters
    p.add_argument("--event-ids", help="Comma-separated EventID list to *include*.")
    p.add_argument("--event-ids-file",
                   help="File with EventID values to *include*, one per line.")
    p.add_argument("--exclude-event-ids",
                   help="Comma-separated EventID list to *exclude*.")
    p.add_argument("--exclude-event-ids-file",
                   help="File with EventID values to *exclude*, one per line.")

    p.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1),
                   help="Parallel worker processes (default: CPU cores – 1).")
    p.add_argument("--placeholder-char", default="§",
                   help="Char that replaces commas inside string fields (default '§').")
    p.add_argument("--logparser", default=DEFAULT_LOGPARSER_PATH,
                   help="Path to LogParser.exe.")
    p.add_argument("--log-file", help="Write errors here (default <output>.log)")
    return p.parse_args()


# ───────────────────────── helper utilities ────────────────────────
def _load_id_list(arg_val: Optional[str], file_path: Optional[str]) -> Optional[List[int]]:
    ids: List[int] = []
    if arg_val:
        ids.extend(int(x.strip()) for x in arg_val.split(',') if x.strip())
    if file_path:
        with open(file_path, encoding="utf-8") as f:
            ids.extend(int(line.strip()) for line in f if line.strip())
    return ids or None


def _list_event_files(root: str) -> List[str]:
    pat = re.compile(r".*\.(evtx?|evt)$", re.I)
    return [os.path.join(dp, f) for dp, _, fs in os.walk(root)
            for f in fs if pat.match(f)]


def _log_error(log_file: str, msg: str) -> None:
    logging.error(msg)
    try:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _build_lp_cmd(lp: str, src: str, dst_xml: str) -> List[str]:
    query = f"SELECT * INTO {dst_xml} FROM '{src}'"
    return [
        lp, query,
        "-i:EVT",          # EVT plug-in parses both .evt and .evtx
        "-o:XML",
        "-structure:1",    # <ROW> wrapper for every record  
        "-q:ON"
    ]


def _filter_frame(df, start, end, incl, excl, placeholder):
    df = df.copy()
    if "TimeGenerated" in df.columns:
        df["TimeGenerated"] = pd.to_datetime(df["TimeGenerated"], errors="coerce")
        df = df[(df["TimeGenerated"] >= start) & (df["TimeGenerated"] <= end)]

    if incl is not None and "EventID" in df.columns:
        df = df[df["EventID"].isin(incl)]
    if excl is not None and "EventID" in df.columns:
        df = df[~df["EventID"].isin(excl)]

    # ---- Force every object cell to displayable str ----------
    obj_cols = df.select_dtypes(include=["object"]).columns
    for col in obj_cols:
        df[col] = df[col].apply(
            lambda x: (x.decode("utf-16le", "ignore")          # bytes → str
                       if isinstance(x, (bytes, bytearray))
                       else str(x))
        ).str.replace(",", placeholder, regex=False)
    return df


def _detect_xml_encoding(raw: bytes) -> str:
    """Return a guessed Python codec name for LogParser XML."""
    # BOM beats everything
    if raw.startswith(b"\xff\xfe"):
        return "utf-16le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16be"

    # Parse the encoding attribute in the XML declaration
    m = ENC_RE.search(raw[:200])
    if m:
        enc = m.group(1).decode("ascii", "ignore").lower()
        if enc in {
            "iso-10646-ucs-2", "utf-16", "utf-16le", "utf-16be",
            "ucs-2", "unicode"
        }:
            return "utf-16le"
        return enc

    # Fallback
    return "utf-8"

def _safe_read_xml(path: str) -> Optional[pd.DataFrame]:
    """
    Parse LogParser XML with unknown encoding.
    Returns DataFrame or None (if no <ROW> elements).
    """
    try:
        # fast path – let pandas try with its own sniffing
        return pd.read_xml(path, xpath="//ROW")
    except (XPathError, ValueError):
        pass          # either no nodes or encoding blew up

    # Manual decode
    with open(path, "rb") as fh:
        raw = fh.read()

    enc = _detect_xml_encoding(raw)
    txt = raw.decode(enc, errors="ignore")

    try:
        return pd.read_xml(StringIO(txt), xpath="//ROW")
    except XPathError:
        return Non

def _safe_copy(src: str, dst_dir: str) -> str:
    """
    Copy or hard-link *src* into *dst_dir* with a name that has no '%'
    characters (Log Parser chokes on them). Returns the new path.
    Uses a hard-link when the source and destination are on the same
    drive, so there’s no 100 GB copy penalty.
    """
    clean_name = re.sub(r"%+", "_", os.path.basename(src))
    if "%" in clean_name or clean_name == os.path.basename(src):
        # keep basename if it had no %
        pass
    tmp_path = os.path.join(dst_dir, f"{uuid.uuid4().hex}_{clean_name}")
    try:
        os.link(src, tmp_path)          # cheap, same-drive hard-link
    except (OSError, AttributeError):
        shutil.copy2(src, tmp_path)     # fallback: real copy
    return tmp_path

# ────────────────────── multiprocessing worker ─────────────────────
def _worker(job: Tuple[str, str, dt.datetime, dt.datetime,
                       Optional[List[int]], Optional[List[int]],
                       str, str]) -> Optional[pd.DataFrame]:
    (fp, lp, sdt, edt, incl, excl, ph, logf) = job
    tmp_dir = tempfile.mkdtemp(prefix="evtfilter_")
    try:
        safe_src = _safe_copy(fp, tmp_dir)        # <- NEW
        tmp_xml  = os.path.join(tmp_dir, "lp.xml")

        # ---------- run Log Parser ----------
        run = subprocess.run(
            _build_lp_cmd(lp, safe_src, tmp_xml),
            capture_output=True, text=True
        )
        if run.returncode:
            _log_error(logf, f"LogParser failed ({run.returncode}) on {fp}:\n"
                             f"STDERR: {run.stderr.strip()}\nSTDOUT: {run.stdout.strip()}")
            return None
        if not os.path.isfile(tmp_xml) or os.path.getsize(tmp_xml) == 0:
            logging.info("%s: log contained 0 events", fp)
            return None

        # ---------- XML → DataFrame ----------
        df = _safe_read_xml(tmp_xml)
        if df is None or df.empty:
            logging.info("%s: no events in selected time-window", fp)
            return None
        df = _filter_frame(df, sdt, edt, incl, excl, ph)
        df["SourceFile"] = fp
        return df

    except Exception as exc:
        _log_error(logf, f"Exception processing {fp}: {exc}")
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ────────────────────────────── main ───────────────────────────────
def main() -> None:
    ns = parse_args()
    ns.log_file = ns.log_file or f"{ns.output}.log"
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(ns.log_file,
                                                      delay=True, encoding="utf-8")])

    start_dt = dt.datetime.strptime(ns.start_date, "%Y-%m-%d %H:%M:%S")
    end_dt = dt.datetime.strptime(ns.end_date, "%Y-%m-%d %H:%M:%S")

    inc_ids = _load_id_list(ns.event_ids, ns.event_ids_file)
    exc_ids = _load_id_list(ns.exclude_event_ids, ns.exclude_event_ids_file)

    files = _list_event_files(ns.dir)
    if not files:
        sys.exit(f"No .evt/.evtx files under {ns.dir}")

    logging.info("Scanning %d files …", len(files))
    pool_args = [(f, ns.logparser, start_dt, end_dt,
                  inc_ids, exc_ids, ns.placeholder_char, ns.log_file)
                 for f in files]

    with mp.Pool(processes=ns.workers) as pool:
        frames = [df for df in pool.map(_worker, pool_args)
                  if df is not None and not df.empty]

    if not frames:
        logging.warning("No matching events found.")
        return

    agg = pd.concat(frames, ignore_index=True)
    # keep SourceFile as last column
    cols = [c for c in agg.columns if c != "SourceFile"] + ["SourceFile"]
    agg[cols].to_csv(ns.output, index=False, quoting=csv.QUOTE_MINIMAL)

    logging.info("Done. %d rows → %s", len(agg), ns.output)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
