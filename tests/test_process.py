"""process_account() with the network faked out."""
from pathlib import Path

import pytest

from cf_receipts import cli

RECEIPT = b"%PDF-1.4 fake receipt"
HISTORY = [
    {"id": "a", "invoice_id": "a", "receipt_id": "IN-1", "amount": 10.46, "amount_remaining": 0,
     "currency": "usd", "occurred_at": "2026-10-09T10:00:00Z"},                       # -> saved
    {"id": "b", "invoice_id": "b", "receipt_id": "IN-2", "amount": None,
     "currency": "usd", "occurred_at": "2026-10-10T10:00:00Z"},                       # $0 -> ignored
    {"id": "c", "invoice_id": "c", "receipt_id": "IN-3", "amount": 5, "amount_remaining": 0,
     "currency": "usd", "occurred_at": "2026-10-11T10:00:00Z"},                       # already present
    {"id": "d", "invoice_id": "d", "receipt_id": "IN-4", "amount": 7, "amount_remaining": 7,
     "currency": "usd", "occurred_at": "2026-10-12T10:00:00Z"},                       # unpaid
    {"id": "e", "invoice_id": "e", "receipt_id": "IN-5", "amount": 9, "amount_remaining": 0,
     "currency": "usd", "occurred_at": "2026-10-13T10:00:00Z"},                       # link expired
]


@pytest.fixture
def fake_network(monkeypatch):
    monkeypatch.setattr(cli, "cf_paginated", lambda path, token: HISTORY)
    monkeypatch.setattr(cli, "fetch_receipt", lambda aid, inv, token: None if inv == "e" else RECEIPT)
    monkeypatch.setattr(cli, "pdf_text", lambda pdf: "Registrar Registration Fee - example.com (1 yr)")


def run(tmp_path: Path, dry_run: bool) -> cli.Stats:
    (tmp_path / "REC_2026-10-11_Cloudflare_5.00USD_Renamed_IN-3.pdf").write_bytes(RECEIPT)
    stats = cli.Stats()
    cli.process_account({"id": "acct", "name": "Test"}, "tok", tmp_path, "REC_", dry_run, stats)
    return stats


def test_process_account(tmp_path, fake_network):
    stats = run(tmp_path, dry_run=False)
    assert (stats.saved, stats.skipped, stats.unpaid, stats.unavailable) == (1, 1, 1, 1)
    saved = tmp_path / "REC_2026-10-09_Cloudflare_10.46USD_DomainRegistration-example.com_IN-1.pdf"
    assert saved.read_bytes() == RECEIPT
    assert len(list(tmp_path.iterdir())) == 2


def test_dry_run_writes_nothing(tmp_path, fake_network):
    stats = run(tmp_path, dry_run=True)
    assert stats.saved == 1
    assert len(list(tmp_path.iterdir())) == 1  # only the pre-existing file
