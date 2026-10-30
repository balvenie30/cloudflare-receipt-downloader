"""cf-receipts: download Cloudflare payment receipts (not invoices) as PDFs.

Cloudflare's dashboard only offers invoices. The receipt lives on the Stripe page
behind each invoice's "Pay online" link. This tool automates that path:

  billing history -> invoice PDF (in memory) -> Stripe "Pay online" link
  -> Stripe receipt URL -> receipt PDF saved to disk
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

API = "https://api.cloudflare.com/client/v4"
USER_AGENT = f"cloudflare-receipt-downloader/{__version__}"
TOKEN_VARS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API")
STRIPE_LINK = re.compile(rb"https://invoice\.stripe\.com/i/(acct_[A-Za-z0-9]+)/([^?)\s/<>\"']+)")
UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, token: str | None = None, timeout: int = 60) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def cf_get(path: str, token: str) -> bytes:
    try:
        return http_get(API + path, token)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            detail = json.loads(body).get("errors") or body[:300]
        except ValueError:
            detail = body[:300]
        raise ApiError(f"Cloudflare API returned HTTP {e.code} for {path}: {detail}") from None


def cf_json(path: str, token: str) -> dict:
    data = json.loads(cf_get(path, token))
    if data.get("success") is False:
        raise ApiError(f"Cloudflare API error for {path}: {data.get('errors')}")
    return data


def cf_paginated(path: str, token: str, per_page: int = 50) -> list[dict]:
    items: list[dict] = []
    page = 1
    sep = "&" if "?" in path else "?"
    while True:
        data = cf_json(f"{path}{sep}page={page}&per_page={per_page}", token)
        items += data.get("result") or []
        info = data.get("result_info") or {}
        has_next = info.get("next_page")
        if has_next is None:
            has_next = page < (info.get("total_pages") or 1)
        if not has_next:
            return items
        page += 1


# --------------------------------------------------------------------------- Stripe

def find_stripe_link(invoice_pdf: bytes) -> tuple[str, str] | None:
    """Return (stripe_account, invoice_token) from an invoice PDF's "Pay online" link."""
    m = STRIPE_LINK.search(invoice_pdf)
    return (m.group(1).decode(), m.group(2).decode()) if m else None


def stripe_receipt_url(stripe_account: str, invoice_token: str) -> str | None:
    """Ask Stripe's hosted-invoice backend for the receipt PDF URL. None if unavailable/expired."""
    url = f"https://invoicedata.stripe.com/invoice_receipt_file_url/{stripe_account}/{invoice_token}?s=ap"
    try:
        file_url = json.loads(http_get(url)).get("file_url")
    except (OSError, ValueError, AttributeError):  # network/HTTP errors, non-JSON, JSON that isn't an object
        return None
    if not file_url:
        return None
    # The returned URL points at dashboard.stripe.com, which needs a Stripe login;
    # the same path on pay.stripe.com serves the PDF publicly.
    return re.sub(r"^https://[^/]+", "https://pay.stripe.com", file_url)


# --------------------------------------------------------------------------- naming

def pdf_text(pdf: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return ""
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    except Exception:
        return ""


def _camel(part: str) -> str:
    part = part.strip()
    if re.fullmatch(r"[\w-]+(\.[\w-]+)+", part):  # looks like a domain: keep as-is
        return part
    return "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", part))


def description_from_text(text: str) -> str:
    """Turn the receipt's first line item into a short file-name-safe description.

    "Registrar Registration Fee - example.com (1 yr)" -> "DomainRegistration-example.com"
    "Pro Plan - example.com"                          -> "ProPlan-example.com"
    """
    m = re.search(r"Registrar (\w+) Fee\s*-\s*([\w.-]+\.[A-Za-z]{2,})", text)
    if m:
        return f"Domain{m.group(1)}-{m.group(2)}"
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("Description"):
            item = next((l for l in lines[i + 1:] if l), "")
            item = re.sub(r"\s+\d+\s+-?\D?[\d.,]+\s.*$|\s+\d+\s+-?\D?[\d.,]+$", "", item)  # drop "qty $price $amount"
            item = re.sub(r"\s*\(.*?\)\s*", " ", item)      # drop "(1 yr)"
            parts = [_camel(p) for p in re.split(r"\s+-\s+", item) if p.strip()]
            desc = "-".join(p for p in parts if p)
            if desc:
                return desc[:60]
    return "Cloudflare"


def safe_part(value: str) -> str:
    """Make an API-supplied value safe to use inside a file name (no paths, no separators)."""
    return re.sub(r"[^A-Za-z0-9.-]+", "", str(value)).strip(".")


def build_filename(prefix: str, date: str, amount: float, currency: str,
                   description: str, invoice_number: str) -> str:
    description = UNSAFE_CHARS.sub("", description) or "Cloudflare"
    return (f"{UNSAFE_CHARS.sub('', prefix)}{date}_Cloudflare_{float(amount):.2f}{safe_part(currency).upper()}"
            f"_{description}_{invoice_number}.pdf")


def already_downloaded(out_dir: Path, invoice_number: str) -> bool:
    return out_dir.is_dir() and any(out_dir.glob(f"*_{glob.escape(invoice_number)}.pdf"))


# --------------------------------------------------------------------------- token

def load_token(cli_token: str | None, env_file: Path) -> str | None:
    if cli_token:
        return cli_token
    for var in TOKEN_VARS:
        if os.environ.get(var):
            return os.environ[var]
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            m = re.match(r'\s*(?:export\s+)?(\w+)\s*=\s*["\']?([^"\'\s#]+)', line)
            if m and m.group(1) in TOKEN_VARS:
                return m.group(2)
    return None


# --------------------------------------------------------------------------- main

@dataclass
class Stats:
    saved: int = 0
    skipped: int = 0
    unpaid: int = 0
    unavailable: int = 0


def fetch_receipt(aid: str, invoice_id: str, token: str) -> bytes | None:
    """Receipt PDF bytes for one invoice, or None if it can't be fetched."""
    try:
        invoice_pdf = cf_get(f"/accounts/{aid}/billing/receipts/{invoice_id}/pdf?doctype=invoice", token)
    except (ApiError, OSError):
        return None
    link = find_stripe_link(invoice_pdf)
    receipt_url = stripe_receipt_url(*link) if link else None
    if not receipt_url:
        return None
    try:
        receipt = http_get(receipt_url)
    except OSError:
        return None
    return receipt if receipt.startswith(b"%PDF-") else None


def process_account(account: dict, token: str, out_dir: Path, prefix: str, dry_run: bool, stats: Stats) -> None:
    aid = account["id"]
    history = cf_paginated(f"/accounts/{aid}/billing/history", token)
    print(f"{account.get('name', aid)}: {len(history)} billing history item(s)")
    for item in history:
        amount = float(item.get("amount") or 0)
        if amount <= 0:
            continue  # $0 / credits: nothing was charged, so Stripe issues no receipt
        invoice_id = item.get("invoice_id") or item["id"]
        number = safe_part(item.get("receipt_id") or invoice_id)  # Cloudflare's "receipt_id" is the invoice number (IN-...)
        if already_downloaded(out_dir, number):
            stats.skipped += 1
            continue
        if float(item.get("amount_remaining") or 0) > 0:
            stats.unpaid += 1
            print(f"  - {number}: not paid yet, no receipt")
            continue

        receipt = fetch_receipt(aid, invoice_id, token)
        if receipt is None:
            stats.unavailable += 1
            print(f"  ! {number}: receipt unavailable (Stripe link missing or expired - ask Cloudflare billing support)")
            continue

        name = build_filename(prefix, safe_part((item.get("occurred_at") or "")[:10]) or "unknown-date",
                              amount, item.get("currency") or "usd",
                              description_from_text(pdf_text(receipt)), number)
        if not dry_run:
            (out_dir / name).write_bytes(receipt)
        stats.saved += 1
        print(f"  {'would save' if dry_run else 'saved'} {name}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cf-receipts",
        description="Download Cloudflare payment receipts (not invoices) as PDFs.")
    p.add_argument("-o", "--out", default="receipts", help="folder to save receipts in (default: ./receipts)")
    p.add_argument("--token", help=f"Cloudflare API token with Billing Read (default: ${TOKEN_VARS[0]} or .env)")
    p.add_argument("--env-file", default=".env", help="file to read the token from (default: ./.env)")
    p.add_argument("--account", action="append", metavar="ID", help="only this account ID (repeatable; default: all)")
    p.add_argument("--prefix", default="REC_", help='file name prefix (default: "REC_"; use "" for none)')
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be saved without writing files")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    token = load_token(args.token, Path(args.env_file))
    if not token:
        p.error(f"no API token: pass --token, set ${TOKEN_VARS[0]}, or put {TOKEN_VARS[0]}=... in {args.env_file}")

    out_dir = Path(args.out).expanduser()
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    stats = Stats()
    try:
        accounts = ([{"id": a} for a in args.account] if args.account
                    else cf_paginated("/accounts", token))
        if not accounts:
            print("No accounts visible to this token.", file=sys.stderr)
            return 1
        for account in accounts:
            process_account(account, token, out_dir, args.prefix, args.dry_run, stats)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"error: network problem: {e.reason}", file=sys.stderr)
        return 1
    except OSError as e:  # timeouts, connection resets, disk errors
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    verb = "would save" if args.dry_run else "saved"
    summary = f"{stats.saved} {verb}, {stats.skipped} already present, {stats.unavailable} unavailable"
    if stats.unpaid:
        summary += f", {stats.unpaid} not paid yet"
    print(f"\nDone: {summary}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
