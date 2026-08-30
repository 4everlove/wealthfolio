#!/usr/bin/env python3
"""Fetch an IBKR Flex Query via the Flex Web Service and optionally convert.

Two-step IBKR flow:
  1. POST SendRequest?t=<token>&q=<query_id>&v=3
       → reference code (aka "code")
  2. GET  GetStatement?t=<token>&q=<reference>&v=3
       (poll every few seconds until ready) → CSV bytes

Writes the raw CSV to --out-dir and (optionally) chains into
ibkr_flex_to_wf.py to produce the Wealthfolio import CSV.

Setup:
  1. IBKR Client Portal → Reports → Settings → Flex Web Service → generate
     token. Save it to a file at 0600, e.g.:
         mkdir -p ~/.wealthfolio/ibkr && chmod 700 ~/.wealthfolio/ibkr
         echo 'YOUR_TOKEN' > ~/.wealthfolio/ibkr/token
         chmod 600 ~/.wealthfolio/ibkr/token
  2. Find the numeric Query ID for your saved Flex Query (shown in the
     Client Portal after you save the query — this is a number like
     `1234567`, not the query name).

Usage:
  python scripts/import/ibkr_flex_fetch.py \\
      --query-id 1234567 \\
      --token-file ~/.wealthfolio/ibkr/token \\
      --out-dir ~/.wealthfolio/ibkr \\
      --run-converter \\
      --seed-from ~/Dropbox/Ledger/docs/Brokers/ibkr_202601_202607.csv \\
      --converter-out ~/Dropbox/Ledger/docs/Brokers/wf_ibkr_latest.csv

Cron (weekly Monday 6am):
  0 6 * * 1 /path/to/python /path/to/ibkr_flex_fetch.py \\
      --query-id 1234567 --token-file ~/.wealthfolio/ibkr/token \\
      --run-converter --converter-out ~/Dropbox/Ledger/docs/Brokers/wf_ibkr_latest.csv
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_URL = f"{BASE_URL}/SendRequest"
GET_URL = f"{BASE_URL}/GetStatement"
USER_AGENT = "wealthfolio-ibkr-flex-fetch/1.0"
API_VERSION = "3"


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def send_request(token: str, query_id: str) -> str:
    """Fire step 1. Returns the reference code used in GetStatement."""
    qs = urllib.parse.urlencode({"t": token, "q": query_id, "v": API_VERSION})
    body = _http_get(f"{SEND_URL}?{qs}")
    root = ET.fromstring(body)
    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        code = (root.findtext("ErrorCode") or "").strip()
        msg = (root.findtext("ErrorMessage") or "").strip()
        raise RuntimeError(f"SendRequest failed: status={status} code={code} msg={msg}")
    ref = (root.findtext("ReferenceCode") or "").strip()
    if not ref:
        raise RuntimeError(f"SendRequest returned no ReferenceCode; body={body[:400]!r}")
    return ref


def get_statement(
    token: str,
    reference_code: str,
    poll_interval: int = 15,
    max_attempts: int = 12,
) -> bytes:
    """Fire step 2 with polling. Returns the CSV bytes.

    IBKR returns an XML error envelope (with ErrorCode 1019 "Statement generation
    in progress") while the report is still being built. Once ready, the body
    is the raw CSV.
    """
    qs = urllib.parse.urlencode({"t": token, "q": reference_code, "v": API_VERSION})
    url = f"{GET_URL}?{qs}"
    for attempt in range(1, max_attempts + 1):
        body = _http_get(url, timeout=60)
        # A ready CSV starts with a quoted header (e.g. `"ClientAccountID"`).
        # An in-progress or error response is an XML doc.
        stripped = body.lstrip()
        if stripped.startswith(b"<"):
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                raise RuntimeError(f"Unexpected non-XML, non-CSV response: {body[:400]!r}")
            code = (root.findtext("ErrorCode") or "").strip()
            msg = (root.findtext("ErrorMessage") or "").strip()
            if code == "1019":
                print(f"  [{attempt}/{max_attempts}] statement in progress, sleeping {poll_interval}s…", flush=True)
                time.sleep(poll_interval)
                continue
            raise RuntimeError(f"GetStatement failed: code={code} msg={msg}")
        return body
    raise RuntimeError(f"GetStatement not ready after {max_attempts} attempts ({max_attempts * poll_interval}s)")


def read_token(token_file: Path | None, token_env: str | None) -> str:
    if token_env:
        v = os.environ.get(token_env)
        if not v:
            raise SystemExit(f"Env var {token_env} not set")
        return v.strip()
    if token_file:
        if not token_file.exists():
            raise SystemExit(f"Token file not found: {token_file}")
        st = token_file.stat()
        # Warn if world/group-readable.
        if st.st_mode & 0o077:
            print(f"WARN: {token_file} is not 0600 (mode={oct(st.st_mode & 0o777)}); "
                  f"run `chmod 600 {token_file}`", file=sys.stderr)
        return token_file.read_text().strip()
    raise SystemExit("Must provide --token-file or --token-env")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--query-id", required=True, help="Numeric Flex Query ID")
    ap.add_argument("--token-file", type=Path, default=Path.home() / ".wealthfolio" / "ibkr" / "token")
    ap.add_argument("--token-env", help="Read token from this env var instead of --token-file")
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".wealthfolio" / "ibkr",
                    help="Directory to write raw CSV")
    ap.add_argument("--out-name", help="Override output filename (default: flex_<query>_<UTC-date>.csv)")
    ap.add_argument("--poll-interval", type=int, default=15, help="Seconds between GetStatement polls (default 15)")
    ap.add_argument("--max-attempts", type=int, default=12, help="Max GetStatement polls (default 12 = 3min)")
    # Optional converter chain
    ap.add_argument("--run-converter", action="store_true",
                    help="After fetch, invoke ibkr_flex_to_wf.py on the CSV")
    ap.add_argument("--converter-out", type=Path,
                    help="Output path for converter (required with --run-converter)")
    ap.add_argument("--seed-from", type=Path,
                    help="Prior-period Flex CSV to pass to converter --seed-from --seed-walk-only")
    ap.add_argument("--account-map", type=Path, default=Path.home() / ".wealthfolio" / "ibkr_accounts.yml")
    args = ap.parse_args()

    if args.run_converter and not args.converter_out:
        raise SystemExit("--converter-out is required with --run-converter")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    token = read_token(args.token_file, args.token_env)

    print(f"Requesting Flex Query {args.query_id}…", flush=True)
    ref = send_request(token, args.query_id)
    print(f"  reference code: {ref}", flush=True)

    print("Fetching statement…", flush=True)
    csv_bytes = get_statement(token, ref, args.poll_interval, args.max_attempts)

    out_name = args.out_name or f"flex_{args.query_id}_{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    out_path = args.out_dir / out_name
    out_path.write_bytes(csv_bytes)
    print(f"Wrote {len(csv_bytes):,} bytes to {out_path}")

    if args.run_converter:
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("ibkr_flex_to_wf.py")),
            str(out_path),
            str(args.converter_out),
            "--account-map", str(args.account_map),
        ]
        if args.seed_from:
            cmd += ["--seed-from", str(args.seed_from), "--seed-walk-only"]
        print(f"Running converter: {' '.join(cmd)}", flush=True)
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
