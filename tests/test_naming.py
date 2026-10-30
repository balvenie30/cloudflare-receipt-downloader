from pathlib import Path

from cf_receipts.cli import (already_downloaded, build_filename, description_from_text,
                             find_stripe_link, load_token)


def test_registrar_description():
    text = "Description Qty Unit price Amount\nRegistrar Registration Fee - example.com (1 yr) 1 $10.46 $10.46"
    assert description_from_text(text) == "DomainRegistration-example.com"
    assert description_from_text("Registrar Transfer Fee - my-site.dev (1 yr)") == "DomainTransfer-my-site.dev"


def test_generic_description():
    text = "Description Qty Unit price Amount\n\nPro Plan - example.com (Monthly) 1 $25.00 $25.00\nSubtotal"
    assert description_from_text(text) == "ProPlan-example.com"
    assert description_from_text("Description\nWorkers Paid 1 €5.00 €5.00") == "WorkersPaid"
    assert description_from_text("no line items here") == "Cloudflare"


def test_unsafe_api_values_are_sanitized():
    from cf_receipts.cli import safe_part
    assert safe_part("../../etc/IN-1") == "etcIN-1"
    assert "/" not in build_filename("../", "2026-01-01", 1, "u/sd", "x", safe_part("IN/1"))


def test_build_filename():
    assert (build_filename("REC_", "2026-10-09", 10.46, "usd", "DomainRegistration-example.com", "IN-1")
            == "REC_2026-10-09_Cloudflare_10.46USD_DomainRegistration-example.com_IN-1.pdf")
    assert build_filename("", "2026-01-01", 160, "eur", "a/b c", "IN-2") == "2026-01-01_Cloudflare_160.00EUR_abc_IN-2.pdf"


def test_find_stripe_link():
    pdf = b"<</URI (https://invoice.stripe.com/i/acct_1ABC/live_YWJj_-x?s=db)>>"
    assert find_stripe_link(pdf) == ("acct_1ABC", "live_YWJj_-x")
    assert find_stripe_link(b"%PDF- nothing") is None


def test_already_downloaded(tmp_path: Path):
    (tmp_path / "REC_2026-01-01_Cloudflare_1.00USD_X_IN-123.pdf").write_bytes(b"%PDF-")
    assert already_downloaded(tmp_path, "IN-123")
    assert not already_downloaded(tmp_path, "IN-12")


def test_load_token(tmp_path: Path, monkeypatch):
    for v in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API"):
        monkeypatch.delenv(v, raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nCLOUDFLARE_API_TOKEN="abc123"\n')
    assert load_token(None, env) == "abc123"
    assert load_token("cli", env) == "cli"
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fromenv")
    assert load_token(None, env) == "fromenv"
