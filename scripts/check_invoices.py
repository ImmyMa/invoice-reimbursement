#!/usr/bin/env python3
"""Check extracted invoice records; never fetch mail or decide tax eligibility.

Money must be JSON strings, identifiers remain strings, and source_refs and
expense_ref are caller-supplied evidence references, not independently verified.
The report is deterministic and an existing report is never overwritten.
"""

import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import tempfile


DISPOSITIONS = (
    "conflict", "duplicate", "existing_claim", "review", "out_of_period", "eligible"
)
DEFAULT_CATEGORIES = ["餐饮", "交通", "住宿", "采购"]
ENUMS = {
    "invoice_type": {"digital", "coded", "unknown"},
    "period_basis": {"consumption", "invoice_confirmed", "manual_confirmed", "unknown"},
    "field_status": {"verified", "pending"},
    "claim_status": {"draft", "submitted", "reimbursed", "unknown"},
    "invoice_status": {"normal", "red", "void", "unknown"},
}
TEXT_FIELDS = (
    "invoice_number", "invoice_code", "seller", "buyer", "currency", "category",
    "project", "purpose",
)
DATE_FIELDS = ("invoice_date", "consumption_date", "period_date")
MONEY_FIELDS = ("gross_amount", "requested_amount")
PLACEHOLDERS = {"待补充", "待确认", "待核对", "未知", "不详", "未提供", "n/a", "na", "null", "none", "-", "?"}


def _date(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"{field}: 日期须为 YYYY-MM-DD 字符串或 null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}: 无效日期") from exc


def _money(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError(f"{field}: 金额须为最多两位小数的十进制字符串或 null")
    # Avoid Decimal's context-dependent addition/formatting for unreasonable inputs.
    if len(value.lstrip("+-").split(".")[0]) > 18:
        raise ValueError(f"{field}: 金额整数部分不得超过18位")
    return Decimal(value)


def _text_present(value):
    text = value.strip().strip("[]【】()（）<>").strip()
    if not text or text.casefold() in PLACEHOLDERS:
        return False
    # Recognize ordinary annotated/template placeholders without treating a word
    # embedded in a genuine company name or narrative as an unknown fact.
    placeholder = r"^(?:(?:项目名称|项目|报销事由|事由|购买方|销售方)\s*[:：]?\s*)?(?:待补充|待确认|待核对|待填写|待用户补充|待用户确认)(?:$|[\s（(\[【:：])"
    return not re.match(placeholder, text) and not text.startswith(("请填写", "请补充"))


def _identity(record):
    number, code = record["invoice_number"], record["invoice_code"]
    if record["invoice_type"] == "digital" and re.fullmatch(r"[0-9]{20}", number):
        return f"digital:{number}"
    if record["invoice_type"] == "coded" and re.fullmatch(r"[0-9]+", code) and re.fullmatch(r"[0-9]+", number):
        return f"coded:{code}:{number}"
    return None


def _normalize(data):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("schema_version 必须为整数1")
    invoices = data.get("invoices")
    if not isinstance(invoices, list):
        raise ValueError("invoices 必须为数组")
    categories = data.get("allowed_categories", DEFAULT_CATEGORIES)
    if not isinstance(categories, list) or not categories or any(not isinstance(c, str) or not _text_present(c) for c in categories):
        raise ValueError("allowed_categories 必须为非空字符串数组")
    if len(set(categories)) != len(categories):
        raise ValueError("allowed_categories 不得含重复项")
    result, seen = [], set()
    required = {"record_id", "source_refs", *TEXT_FIELDS, *DATE_FIELDS, *MONEY_FIELDS, *ENUMS}
    for index, item in enumerate(invoices):
        if not isinstance(item, dict):
            raise ValueError(f"invoices[{index}] 必须为对象")
        if required - item.keys():
            raise ValueError(f"invoices[{index}] 缺少字段: {', '.join(sorted(required - item.keys()))}")
        record = dict(item)
        record_id = record["record_id"]
        if not isinstance(record_id, str) or not record_id.strip() or record_id != record_id.strip() or record_id in seen:
            raise ValueError(f"invoices[{index}].record_id 必须为不重复的非空字符串，首尾不得有空白")
        seen.add(record_id)
        for field, choices in ENUMS.items():
            if not isinstance(record[field], str) or record[field] not in choices:
                raise ValueError(f"{record_id}.{field}: 非法枚举值")
        for field in TEXT_FIELDS:
            if not isinstance(record[field], str):
                raise ValueError(f"{record_id}.{field}: 必须为字符串，未知文本用空字符串")
            record[field] = record[field].strip()
        if not re.fullmatch(r"[A-Z]{3}", record["currency"]):
            raise ValueError(f"{record_id}.currency: 必须为三位大写币种代码，不得猜测币种")
        for field in DATE_FIELDS:
            record[field] = _date(record[field], f"{record_id}.{field}")
        for field in MONEY_FIELDS:
            record[field] = _money(record[field], f"{record_id}.{field}")
        sources = record["source_refs"]
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or not s.strip() for s in sources):
            raise ValueError(f"{record_id}.source_refs: 必须为非空来源字符串数组")
        record["source_refs"] = sorted(set(s.strip() for s in sources))
        reason = record.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError(f"{record_id}.reason: 必须为字符串")
        record["reason"] = reason.strip()
        expense_ref = record.get("expense_ref", "")
        if not isinstance(expense_ref, str):
            raise ValueError(f"{record_id}.expense_ref: 必须为字符串")
        record["expense_ref"] = expense_ref.strip()
        record["identity_key"] = _identity(record)
        result.append(record)
    return sorted(result, key=lambda r: r["record_id"]), set(categories)


def _review_reasons(record, categories):
    reasons = []
    if record["identity_key"] is None:
        reasons.append("invoice_identity_unconfirmed")
    for field in ("seller", "buyer", "project", "purpose"):
        if not _text_present(record[field]):
            reasons.append(f"missing_or_placeholder_{field}")
    if record["category"] not in categories:
        reasons.append("category_not_allowed")
    if record["invoice_date"] is None:
        reasons.append("invoice_date_missing")
    gross, requested = record["gross_amount"], record["requested_amount"]
    if gross is None or requested is None:
        reasons.append("amount_missing")
    elif gross < 0 or requested < 0 or requested > gross:
        reasons.append("amount_out_of_bounds")
    if record["field_status"] != "verified":
        reasons.append("fields_not_verified")
    if record["claim_status"] != "draft":
        reasons.append("claim_status_unconfirmed")
    if record["invoice_status"] != "normal":
        reasons.append(f"invoice_status_{record['invoice_status']}")
    basis, period = record["period_basis"], record["period_date"]
    if basis == "unknown" or period is None:
        reasons.append("period_unconfirmed")
    elif basis == "consumption" and (record["consumption_date"] is None or period != record["consumption_date"]):
        reasons.append("period_consumption_date_mismatch")
    elif basis == "invoice_confirmed" and (record["invoice_date"] is None or period != record["invoice_date"]):
        reasons.append("period_invoice_date_mismatch")
    elif basis == "manual_confirmed" and not _text_present(record["reason"]):
        reasons.append("manual_period_reason_missing")
    return reasons


def _fingerprint(record, fields):
    return tuple(record[field] for field in fields)


def _valid_amount(record):
    gross, requested = record["gross_amount"], record["requested_amount"]
    return gross is not None and requested is not None and gross >= 0 and 0 <= requested <= gross


def analyze(data, start, end):
    """Return a deterministic report without modifying data or reading source files."""
    first, last = _date(start, "start"), _date(end, "end")
    if first is None or last is None or first > last:
        raise ValueError("起止日期不能为空，且 start 不得晚于 end")
    records, categories = _normalize(data)
    rows = {r["record_id"]: {"disposition": None, "reasons": [], "canonical_record_id": r["record_id"], "group_source_refs": r["source_refs"]} for r in records}
    groups = defaultdict(list)
    for record in records:
        if record["identity_key"]:
            groups[record["identity_key"]].append(record)
    critical = ("gross_amount", "currency", "seller", "buyer", "invoice_date")
    other = ("requested_amount", "consumption_date", "period_date", "period_basis", "reason", "expense_ref", "category", "project", "purpose", "field_status", "invoice_status", "claim_status")
    for key, group in sorted(groups.items()):
        canonical = group[0]["record_id"]
        sources = sorted({s for record in group for s in record["source_refs"]})
        for record in group:
            rows[record["record_id"]].update(canonical_record_id=canonical, group_source_refs=sources)
        critical_conflict = len({_fingerprint(r, critical) for r in group}) > 1
        already_claimed = any(r["claim_status"] in {"submitted", "reimbursed"} for r in group)
        other_conflict = len({_fingerprint(r, other) for r in group}) > 1
        if critical_conflict:
            state, reason = "conflict", "same_invoice_key_conflicting_invoice_fields"
        elif already_claimed:
            state, reason = "existing_claim", "same_invoice_already_submitted_or_reimbursed"
        elif other_conflict:
            state, reason = "conflict", "same_invoice_key_conflicting_processing_fields"
        else:
            state, reason = None, None
        if state:
            for record in group:
                rows[record["record_id"]].update(disposition=state, reasons=[reason])
        else:
            for record in group[1:]:
                rows[record["record_id"]].update(disposition="duplicate", reasons=["same_confirmed_invoice_identity"])

    fuzzy = defaultdict(list)
    for record in records:
        if record["seller"] and record["buyer"] and record["invoice_date"] and record["gross_amount"] is not None:
            fuzzy[_fingerprint(record, critical)].append(record)
    suspected = set()
    for group in fuzzy.values():
        logical_ids = {r["identity_key"] or f"weak-record:{r['record_id']}" for r in group}
        refs_by_identity = defaultdict(set)
        for record in group:
            refs_by_identity[record["identity_key"]].add(record["expense_ref"])
        # Different invoice numbers do not establish different underlying expenses.
        # Clear this candidate group only when every strong identity consistently
        # cites a different, non-placeholder independently checked expense reference.
        distinct_expenses = (
            all(r["identity_key"] is not None for r in group)
            and all(len(refs) == 1 and _text_present(next(iter(refs))) for refs in refs_by_identity.values())
            and len({next(iter(refs)) for refs in refs_by_identity.values()}) == len(refs_by_identity)
        )
        if len(logical_ids) > 1 and not distinct_expenses:
            suspected.update(r["record_id"] for r in group)

    output = []
    sums = defaultdict(lambda: {state: Decimal("0.00") for state in DISPOSITIONS})
    unknown = Counter()
    invalid = Counter()
    for record in records:
        row = rows[record["record_id"]]
        if row["disposition"] is None:
            reasons = _review_reasons(record, categories)
            if record["record_id"] in suspected:
                reasons.append("suspected_duplicate_requires_review")
            # An existing-claim row without a usable identity still cannot be reused.
            if record["claim_status"] in {"submitted", "reimbursed"}:
                row.update(disposition="existing_claim", reasons=["already_submitted_or_reimbursed"])
            elif reasons:
                row.update(disposition="review", reasons=reasons)
            elif not first <= record["period_date"] <= last:
                row.update(disposition="out_of_period", reasons=["confirmed_period_date_outside_range"])
            else:
                row.update(disposition="eligible", reasons=[])
        currency = record["currency"]
        # Materialize a per-currency bucket even when every amount is unknown.
        bucket = sums[currency]
        if _valid_amount(record):
            bucket[row["disposition"]] += record["requested_amount"]
        elif record["requested_amount"] is None:
            unknown[currency] += 1
        else:
            invalid[currency] += 1
        output.append({
            "record_id": record["record_id"], **row,
            "identity_key": record["identity_key"], "source_refs": record["source_refs"],
            "expense_ref": record["expense_ref"],
            "currency": currency,
            "gross_amount": None if record["gross_amount"] is None else f"{record['gross_amount']:.2f}",
            "requested_amount": None if record["requested_amount"] is None else f"{record['requested_amount']:.2f}",
            "period_date": None if record["period_date"] is None else record["period_date"].isoformat(),
        })
    counts = {state: sum(r["disposition"] == state for r in output) for state in DISPOSITIONS}
    totals = {}
    for currency, amounts in sorted(sums.items()):
        totals[currency] = {
            "eligible_requested_total": f"{amounts['eligible']:.2f}",
            "review_known_requested_total": f"{amounts['review'] + amounts['conflict']:.2f}",
            "known_valid_requested_total": f"{sum(amounts.values(), Decimal('0.00')):.2f}",
            "requested_totals_by_disposition": {k: f"{v:.2f}" for k, v in amounts.items()},
            "unknown_requested_amount_count": unknown[currency],
            "invalid_or_unbounded_amount_count": invalid[currency],
        }
    return {
        "schema_version": 1, "report_kind": "invoice_check_report",
        "status": "needs_review" if counts["review"] or counts["conflict"] else "checked",
        "period": {"start": first.isoformat(), "end": last.isoformat()},
        "counts": {"input": len(records), **counts}, "totals_by_currency": totals,
        "records": output,
        "limitations": [
            "Checks extracted records only; source_refs and field_status are caller-supplied.",
            "expense_ref must cite checked independent order/transaction evidence; the script checks reference consistency, not evidence authenticity.",
            "eligible means passed these record checks, not approved, reimbursed, or tax-verified.",
            "Review totals sum review/conflict records and may include unresolved duplicate candidates; they are not claimable totals.",
        ],
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象含重复字段: {key}")
        result[key] = value
    return result


def _write_new_report(path, report):
    """Publish a complete report atomically and without replacing any path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".invoice-check-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # fails if a file, directory, or symlink exists
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        if os.path.lexists(args.out):
            raise ValueError("报告路径已存在，请指定新文件；不会覆盖已有报告")
        with args.input.open(encoding="utf-8-sig") as stream:
            data = json.load(stream, object_pairs_hook=_unique_object)
        report = analyze(data, args.start, args.end)
        _write_new_report(args.out, report)
        print(json.dumps({"status": report["status"], "counts": report["counts"]}, ensure_ascii=False))
        return 2 if report["status"] == "needs_review" else 0
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"校验失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
