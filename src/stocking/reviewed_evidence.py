"""Import explicitly reviewed official quarterly EPS, without deriving Q4.

This validates an evidence contract, not the truth of a reviewer's interpretation.
The source, financial vintage, review time and original units remain auditable.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import math
from pathlib import Path
from urllib.parse import urlsplit


class EvidenceError(ValueError):
    pass


def utc(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError("invalid_evidence_timestamp") from exc
    if result.tzinfo is None:
        raise EvidenceError("evidence_timestamp_requires_timezone")
    return result.astimezone(timezone.utc)


def ensure_observed_by_cutoff(evidence, information_cutoff):
    """Check nested selected-source observations and quote eligibility times."""
    cutoff = utc(information_cutoff)
    fields = {"retrieved_at", "reviewed_at", "information_cutoff", "eligible_at",
              "session_close_at", "session_completed_plus15min_at", "regular_market_timestamp"}
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in fields and item is not None and utc(item) > cutoff:
                    raise EvidenceError("selected_source_or_quote_after_information_cutoff")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(evidence)


def merge_reviewed_quarters(existing, reviewed):
    """Latest financial vintage wins; a latest same-day conflict cannot fallback."""
    combined = list(existing) + list(reviewed)
    for end in {row["end"] for row in reviewed}:
        candidates = [r for r in combined if r["end"] == end and 84 <= r["duration_days"] <= 98]
        filed = max(r["filed"] for r in candidates)
        latest = [r for r in candidates if r["filed"] == filed]
        if len({(r["start"], r["end"], r["val"]) for r in latest}) != 1:
            raise EvidenceError("reviewed_quarter_conflicts_with_latest_official_observation")
    return combined


def validate_reviewed_quarters(entries, *, symbol, cik, share_basis, as_of,
                              information_cutoff, base_dir):
    """Return SEC-shaped actual observations; never change EPS or share units."""
    if not isinstance(entries, list):
        raise EvidenceError("reviewed_quarters_must_be_list")
    cutoff = utc(information_cutoff)
    if cutoff.date().isoformat() != as_of:
        raise EvidenceError("reviewed_quarter_cutoff_date_mismatch")
    root = Path(base_dir).resolve()
    result, signatures = [], {}
    for original in entries:
        if not isinstance(original, dict):
            raise EvidenceError("reviewed_quarter_must_be_object")
        row = deepcopy(original)
        required = {"symbol": symbol, "cik": cik, "share_basis": share_basis,
                    "eps_basis": "GAAP_diluted", "currency": "USD", "unit": "USD/shares",
                    "earnings_scope": "total", "review_status": "reviewed",
                    "observation_kind": "reported_quarterly_diluted_eps"}
        if any(row.get(key) != value for key, value in required.items()):
            raise EvidenceError("reviewed_quarter_identity_or_basis_mismatch")
        if "source_accession" in row and (not isinstance(row["source_accession"], str) or not row["source_accession"].strip()):
            raise EvidenceError("reviewed_quarter_accession_invalid")
        if isinstance(row.get("eps"), bool) or not isinstance(row.get("eps"), (float, int)) or not math.isfinite(row["eps"]):
            raise EvidenceError("reviewed_quarter_eps_invalid")
        try:
            start, end, published = [date.fromisoformat(row[key]) for key in
                                     ("period_start", "period_end", "financial_available_at")]
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceError("reviewed_quarter_period_invalid") from exc
        if not 84 <= (end - start).days + 1 <= 98 or not start <= end <= published <= cutoff.date():
            raise EvidenceError("reviewed_quarter_not_available_single_quarter")
        observed, reviewed = utc(row.get("retrieved_at")), utc(row.get("reviewed_at"))
        if observed > reviewed or reviewed > cutoff or observed.date() < published:
            raise EvidenceError("reviewed_quarter_observation_or_review_after_cutoff")
        url = urlsplit(row.get("source_url", ""))
        if url.scheme != "https" or not url.hostname or row.get("source_kind") not in {"sec_filing", "issuer_release"}:
            raise EvidenceError("reviewed_quarter_requires_official_source")
        if row["source_kind"] == "sec_filing" and (url.hostname not in {"www.sec.gov", "sec.gov"}
                or not url.path.startswith(f"/Archives/edgar/data/{int(cik)}/")):
            raise EvidenceError("reviewed_quarter_sec_source_identity_mismatch")
        for key in ("source_file", "source_sha256", "evidence_locator", "basis_evidence", "rationale"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise EvidenceError("reviewed_quarter_missing_source_or_review_evidence")
        path = (root / row["source_file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise EvidenceError("reviewed_quarter_source_missing_or_outside_evidence_root")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["source_sha256"]:
            raise EvidenceError("reviewed_quarter_source_hash_mismatch")
        key = (row["period_end"], row["financial_available_at"])
        signature = (row["period_start"], row["eps"])
        if key in signatures and signatures[key] != signature:
            raise EvidenceError("reviewed_quarter_same_day_conflict")
        signatures[key] = signature
        row.update(source_file=str(path), source_hash_verified=True, share_basis_verified=False)
        result.append({"start": start.isoformat(), "end": end.isoformat(), "val": row["eps"],
                       "filed": published.isoformat(), "duration_days": (end - start).days + 1,
                       "accn": row.get("source_accession") or "reviewed-" + row["source_sha256"][:16],
                       "form": row.get("form", "issuer_release"), "tag": "EarningsPerShareDiluted",
                       "source_url": row["source_url"], "reviewed_quarter_evidence": row})
    return result
