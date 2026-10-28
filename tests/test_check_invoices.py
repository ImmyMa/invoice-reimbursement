"""Behavioral regression tests using synthetic records; no real invoices."""

from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_invoices.py"
SPEC = importlib.util.spec_from_file_location("invoice_checker", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def invoice(record_id="r1", **changes):
    value = {
        "record_id": record_id, "invoice_type": "digital",
        "invoice_number": "00123456789012345678", "invoice_code": "",
        "seller": "合成卖方", "buyer": "合成购方",
        "invoice_date": "2026-10-10", "consumption_date": "2026-10-09",
        "period_date": "2026-10-09", "period_basis": "consumption",
        "gross_amount": "100.00", "requested_amount": "80.00", "currency": "CNY",
        "category": "交通", "project": "合成项目", "purpose": "客户现场交通",
        "field_status": "verified", "claim_status": "draft", "invoice_status": "normal",
        "source_refs": ["synthetic.pdf#page=1"],
    }
    value.update(changes)
    return value


def run(*items, **config):
    return checker.analyze({"schema_version": 1, "invoices": list(items), **config}, "2026-10-01", "2026-10-31")


class Checks(unittest.TestCase):
    def test_valid_partial_claim_and_zero(self):
        report = run(invoice(), invoice("r2", invoice_number="00123456789012345679", gross_amount="0", requested_amount="0"))
        self.assertEqual(report["counts"]["eligible"], 2)
        self.assertEqual(report["totals_by_currency"]["CNY"]["eligible_requested_total"], "80.00")
        self.assertTrue(report["records"][0]["identity_key"].endswith("00123456789012345678"))

    def test_decimal_precision(self):
        report = run(invoice(requested_amount="0.10", expense_ref="订单材料A#订单1"), invoice("r2", invoice_number="00123456789012345679", requested_amount="0.20", expense_ref="订单材料A#订单2"))
        self.assertEqual(report["totals_by_currency"]["CNY"]["eligible_requested_total"], "0.30")

    def test_malformed_money_rejected(self):
        for value in (1.2, True, "NaN", "Infinity", "1e2", "1.001", "1,000", "", "1.000", "1000000000000000000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run(invoice(requested_amount=value))

    def test_negative_and_excess_are_review(self):
        for changes in ({"requested_amount": "-1.00"}, {"gross_amount": "-5.00"}, {"requested_amount": "100.01"}):
            with self.subTest(changes=changes):
                report = run(invoice(**changes))
                self.assertEqual(report["counts"]["review"], 1)
                self.assertEqual(report["totals_by_currency"]["CNY"]["eligible_requested_total"], "0.00")

    def test_missing_amount_is_not_zero(self):
        report = run(invoice(requested_amount=None))
        self.assertEqual(report["records"][0]["requested_amount"], None)
        self.assertEqual(report["totals_by_currency"]["CNY"]["unknown_requested_amount_count"], 1)

    def test_currency_separation(self):
        report = run(invoice(), invoice("r2", invoice_number="00123456789012345679", currency="USD", requested_amount="5.25"))
        self.assertEqual(report["totals_by_currency"]["CNY"]["eligible_requested_total"], "80.00")
        self.assertEqual(report["totals_by_currency"]["USD"]["eligible_requested_total"], "5.25")

    def test_different_sources_same_invoice_once(self):
        report = run(invoice("z", source_refs=["chat.pdf#page=1"]), invoice("a", source_refs=["mail.pdf#page=1"]))
        self.assertEqual(report["counts"]["eligible"], 1)
        self.assertEqual(report["counts"]["duplicate"], 1)
        self.assertEqual(report["records"][0]["record_id"], "a")
        self.assertEqual(report["records"][1]["canonical_record_id"], "a")
        self.assertEqual(report["records"][0]["group_source_refs"], ["chat.pdf#page=1", "mail.pdf#page=1"])
        self.assertEqual(report["totals_by_currency"]["CNY"]["eligible_requested_total"], "80.00")

    def test_same_short_number_different_code_not_duplicate(self):
        report = run(invoice(invoice_type="coded", invoice_code="001234", invoice_number="00001234"), invoice("r2", invoice_type="coded", invoice_code="009876", invoice_number="00001234"))
        self.assertEqual(report["counts"]["review"], 2)
        self.assertEqual(report["counts"]["duplicate"], 0)

    def test_different_codes_with_independent_expense_evidence(self):
        report = run(invoice(invoice_type="coded", invoice_code="001234", invoice_number="00001234", expense_ref="订单材料A#订单1"), invoice("r2", invoice_type="coded", invoice_code="009876", invoice_number="00001234", expense_ref="订单材料A#订单2"))
        self.assertEqual(report["counts"]["eligible"], 2)
        self.assertEqual(report["counts"]["duplicate"], 0)

    def test_different_strong_numbers_without_expense_evidence_need_review(self):
        report = run(invoice(), invoice("r2", invoice_number="00123456789012345679"))
        self.assertEqual(report["counts"]["review"], 2)
        self.assertEqual(report["counts"]["duplicate"], 0)
        self.assertTrue(all("suspected_duplicate_requires_review" in r["reasons"] for r in report["records"]))

    def test_same_expense_reference_does_not_clear_different_invoices(self):
        report = run(invoice(expense_ref="订单材料A#订单1"), invoice("r2", invoice_number="00123456789012345679", expense_ref="订单材料A#订单1"))
        self.assertEqual(report["counts"]["review"], 2)
        self.assertEqual(report["counts"]["eligible"], 0)

    def test_different_checked_expense_references_clear_candidate_group(self):
        report = run(invoice(expense_ref="订单材料A#订单1"), invoice("r2", invoice_number="00123456789012345679", expense_ref="订单材料A#订单2"))
        self.assertEqual(report["counts"]["eligible"], 2)
        self.assertEqual(report["totals_by_currency"]["CNY"]["eligible_requested_total"], "160.00")
        self.assertEqual([r["expense_ref"] for r in report["records"]], ["订单材料A#订单1", "订单材料A#订单2"])

    def test_pure_source_aliases_do_not_require_expense_reference(self):
        report = run(invoice(), invoice("r2", source_refs=["另一入口.pdf#page=1"]))
        self.assertEqual(report["counts"]["eligible"], 1)
        self.assertEqual(report["counts"]["duplicate"], 1)
        self.assertEqual(report["counts"]["review"], 0)

    def test_weak_identity_cannot_be_cleared_with_expense_references(self):
        report = run(invoice(expense_ref="订单材料A#订单1"), invoice("r2", invoice_type="unknown", expense_ref="订单材料A#订单2"))
        self.assertEqual(report["counts"]["review"], 2)
        self.assertEqual(report["counts"]["eligible"], 0)

    def test_expense_reference_must_be_consistent_across_source_aliases(self):
        report = run(invoice(expense_ref="订单材料A#订单1"), invoice("r2", expense_ref="订单材料A#订单2"))
        self.assertEqual(report["counts"]["conflict"], 2)
        self.assertEqual(report["counts"]["eligible"], 0)

    def test_placeholder_expense_reference_does_not_clear_candidates(self):
        for value in ("", "待补充", "待确认（订单号）"):
            with self.subTest(value=value):
                report = run(invoice(expense_ref="订单材料A#订单1"), invoice("r2", invoice_number="00123456789012345679", expense_ref=value))
                self.assertEqual(report["counts"]["review"], 2)

    def test_expense_reference_type_and_missing_default(self):
        with self.assertRaises(ValueError):
            run(invoice(expense_ref=None))
        self.assertEqual(run(invoice())["records"][0]["expense_ref"], "")

    def test_strong_identity_conflicting_invoice_fields(self):
        for changes in ({"gross_amount": "101.00"}, {"currency": "USD"}, {"seller": "另一合成卖方"}, {"buyer": "另一合成购方"}, {"invoice_date": "2026-10-11"}):
            with self.subTest(changes=changes):
                report = run(invoice(), invoice("r2", **changes))
                self.assertEqual(report["counts"]["conflict"], 2)
                self.assertEqual(report["counts"]["eligible"], 0)

    def test_processing_conflicts_do_not_choose_favorable_record(self):
        for changes in ({"requested_amount": "90.00"}, {"period_date": "2026-10-08"}, {"field_status": "pending"}, {"invoice_status": "void"}, {"category": "采购"}, {"claim_status": "unknown"}):
            with self.subTest(changes=changes):
                report = run(invoice(), invoice("r2", **changes))
                self.assertEqual(report["counts"]["conflict"], 2)
                self.assertEqual(report["counts"]["eligible"], 0)

    def test_existing_claim_blocks_new_alias(self):
        for status in ("submitted", "reimbursed"):
            with self.subTest(status=status):
                report = run(invoice(), invoice("r2", claim_status=status))
                self.assertEqual(report["counts"]["existing_claim"], 2)
                self.assertEqual(report["counts"]["eligible"], 0)

    def test_existing_claim_without_identity_still_excluded(self):
        report = run(invoice(invoice_type="unknown", claim_status="reimbursed"))
        self.assertEqual(report["counts"]["existing_claim"], 1)

    def test_unknown_identity_is_never_auto_merged(self):
        report = run(invoice(invoice_type="unknown"), invoice("r2", invoice_type="unknown"))
        self.assertEqual(report["counts"]["review"], 2)
        self.assertEqual(report["counts"]["duplicate"], 0)
        self.assertTrue(all("suspected_duplicate_requires_review" in r["reasons"] for r in report["records"]))

    def test_weak_alias_makes_strong_candidate_review(self):
        report = run(invoice(), invoice("r2", invoice_type="unknown"))
        self.assertEqual(report["counts"]["review"], 2)

    def test_single_weak_identity_needs_review(self):
        for changes in ({"invoice_number": "123"}, {"invoice_type": "coded", "invoice_code": "", "invoice_number": "00001234"}):
            with self.subTest(changes=changes):
                self.assertEqual(run(invoice(**changes))["counts"]["review"], 1)

    def test_cross_period_uses_confirmed_consumption_date(self):
        report = run(invoice(consumption_date="2026-09-30", period_date="2026-09-30"))
        self.assertEqual(report["counts"]["out_of_period"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)

    def test_missing_consumption_date_is_not_invoice_date(self):
        report = run(invoice(consumption_date=None, period_date=None, period_basis="unknown"))
        self.assertEqual(report["counts"]["review"], 1)
        confirmed = run(invoice(consumption_date=None, period_date="2026-10-10", period_basis="invoice_confirmed"))
        self.assertEqual(confirmed["counts"]["eligible"], 1)

    def test_period_evidence_must_match(self):
        for changes in ({"period_date": "2026-10-10"}, {"period_basis": "invoice_confirmed"}, {"period_basis": "manual_confirmed"}, {"period_basis": "manual_confirmed", "reason": "待补充"}):
            with self.subTest(changes=changes):
                self.assertEqual(run(invoice(**changes))["counts"]["review"], 1)
        self.assertEqual(run(invoice(period_basis="manual_confirmed", reason="用户确认按业务结算日归属"))["counts"]["eligible"], 1)

    def test_placeholders_invalid_categories_and_statuses(self):
        for changes in ({"project": "待补充"}, {"purpose": "待确认"}, {"category": "通讯"}, {"field_status": "pending"}, {"invoice_status": "red"}, {"invoice_status": "void"}, {"claim_status": "unknown"}, {"invoice_date": None}):
            with self.subTest(changes=changes):
                self.assertEqual(run(invoice(**changes))["counts"]["review"], 1)
        self.assertEqual(run(invoice(category="通讯"), allowed_categories=["通讯"])["counts"]["eligible"], 1)

    def test_annotated_placeholders_remain_pending(self):
        for text in ("待补充（需用户确认）", "【待确认】", "报销事由：待补充", "请填写项目名称"):
            with self.subTest(text=text):
                report = run(invoice(purpose=text))
                self.assertEqual(report["counts"]["review"], 1)
                self.assertEqual(report["counts"]["eligible"], 0)

    def test_date_boundaries_and_invalid_dates(self):
        for day in ("2026-10-01", "2026-10-31"):
            self.assertEqual(run(invoice(consumption_date=day, period_date=day))["counts"]["eligible"], 1)
        for day in ("2026-02-30", "20261010", "10/10/2026"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                run(invoice(invoice_date=day))
        with self.assertRaises(ValueError):
            checker.analyze({"schema_version": 1, "invoices": []}, "2026-11-01", "2026-10-31")

    def test_unique_record_ids_and_string_identifiers(self):
        for items in ((invoice(), invoice()), (invoice(invoice_number=123),)):
            with self.assertRaises(ValueError):
                run(*items)

    def test_schema_and_sources(self):
        for changes in ({"source_refs": []}, {"source_refs": [""]}, {"currency": "cny"}, {"field_status": "maybe"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                run(invoice(**changes))
        with self.assertRaises(ValueError):
            checker.analyze({"schema_version": True, "invoices": []}, "2026-10-01", "2026-10-31")

    def test_deterministic_order_and_no_mutation(self):
        records = [invoice("z"), invoice("a", source_refs=["other.pdf"]), invoice("u", invoice_type="unknown", seller="不同商户")]
        data = {"schema_version": 1, "invoices": records}
        before = deepcopy(data)
        first = checker.analyze(data, "2026-10-01", "2026-10-31")
        self.assertEqual(data, before)
        self.assertEqual(first, checker.analyze(data, "2026-10-01", "2026-10-31"))
        self.assertEqual(first, run(*reversed(records)))

    def test_count_and_money_partition(self):
        report = run(invoice("a"), invoice("b"), invoice("c", invoice_type="unknown", seller="其他卖方"), invoice("d", invoice_number="00123456789012345679", seller="历史商户", claim_status="reimbursed"), invoice("e", invoice_number="00123456789012345670", seller="缺额商户", requested_amount=None))
        self.assertEqual(report["counts"]["input"], sum(report["counts"][s] for s in checker.DISPOSITIONS))
        total = report["totals_by_currency"]["CNY"]
        self.assertEqual(Decimal(total["known_valid_requested_total"]), sum(map(Decimal, total["requested_totals_by_disposition"].values())))
        self.assertEqual(total["eligible_requested_total"], "80.00")
        self.assertEqual(total["review_known_requested_total"], "80.00")


class CLI(unittest.TestCase):
    def invoke(self, source, out):
        return subprocess.run([sys.executable, str(SCRIPT), str(source), "--start", "2026-10-01", "--end", "2026-10-31", "--out", str(out)], capture_output=True, text=True)

    def test_exit_codes_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            source, out = Path(root) / "input.json", Path(root) / "report.json"
            source.write_text(json.dumps({"schema_version": 1, "invoices": [invoice()]}), encoding="utf-8")
            self.assertEqual(self.invoke(source, out).returncode, 0)
            original = out.read_bytes()
            source.write_text("broken", encoding="utf-8")
            second = self.invoke(source, out)
            self.assertEqual(second.returncode, 1)
            self.assertEqual(out.read_bytes(), original)
            self.assertIn("已存在", second.stderr)
            source.write_text(json.dumps({"schema_version": 1, "invoices": [invoice(purpose="待补充")]}), encoding="utf-8")
            new_out = Path(root) / "review.json"
            self.assertEqual(self.invoke(source, new_out).returncode, 2)
            self.assertEqual(json.loads(new_out.read_text())["status"], "needs_review")

    def test_invalid_input_leaves_no_report(self):
        with tempfile.TemporaryDirectory() as root:
            source, out = Path(root) / "input.json", Path(root) / "report.json"
            source.write_text("broken", encoding="utf-8")
            self.assertEqual(self.invoke(source, out).returncode, 1)
            self.assertFalse(out.exists())
            source.write_text('{"schema_version":1,"schema_version":1,"invoices":[]}', encoding="utf-8")
            self.assertEqual(self.invoke(source, out).returncode, 1)
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(root).glob(".invoice-check-*")), [])

    def test_repeated_new_reports_are_identical(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "input.json"
            source.write_text(json.dumps({"schema_version": 1, "invoices": [invoice()]}), encoding="utf-8")
            first, second = Path(root) / "first.json", Path(root) / "second.json"
            self.assertEqual(self.invoke(source, first).returncode, 0)
            self.assertEqual(self.invoke(source, second).returncode, 0)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
