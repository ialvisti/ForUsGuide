"""
Tests del arnés diferencial real (plan de finalización, Tarea 9 Paso 4).

Las pruebas fallan si el runner nunca llama a AMBOS sistemas, si publica un
resultado inseguro/shadow, si omite inquiries o si el veredicto ignora un
umbral aprobado.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# rag-testing no es un paquete importable (guion en el nombre); cargar por path
_DIFF_PATH = Path(__file__).resolve().parent.parent / "rag-testing" / "ticket_differential.py"
_spec = importlib.util.spec_from_file_location("ticket_differential", _DIFF_PATH)
ticket_differential = importlib.util.module_from_spec(_spec)
sys.modules["ticket_differential"] = ticket_differential
_spec.loader.exec_module(ticket_differential)

run_differential = ticket_differential.run_differential
load_thresholds = ticket_differential.load_thresholds


def _matching_result(**over):
    base = {
        "state": "succeeded", "next_action": "send_participant_reply",
        "all_inquiries_safe": True, "published": False, "fallback": False,
        "total_inquiries": 2, "modules": ["balance", "vesting"],
        "forusbots_job_ids": ["fb-1"], "token_limit": 5500,
        "deterministic_facts": {"balance": 1000.0},
        "reply_text": "here is your rollover information and next steps",
    }
    base.update(over)
    return base


def _runner(result):
    async def _run(case):
        return dict(result)
    return _run


class TestDifferentialHarness:

    async def test_calls_both_systems(self):
        legacy_calls, v2_calls = [], []

        async def legacy(case):
            legacy_calls.append(case["case_id"])
            return _matching_result()

        async def v2(case):
            v2_calls.append(case["case_id"])
            return _matching_result()

        cases = [{"case_id": "c1"}, {"case_id": "c2"}]
        report = await run_differential(cases, legacy, v2)
        assert legacy_calls == ["c1", "c2"], "el legacy nunca se invocó"
        assert v2_calls == ["c1", "c2"], "el v2 nunca se invocó"
        assert report["cases"] == 2

    async def test_empty_cases_is_error(self):
        with pytest.raises(ValueError):
            await run_differential([], _runner({}), _runner({}))

    async def test_matching_systems_pass_all_thresholds(self):
        cases = [{"case_id": "c1"}]
        report = await run_differential(
            cases, _runner(_matching_result()), _runner(_matching_result()))
        assert report["passed"], report["failures"]

    async def test_unsafe_publish_fails(self):
        """v2 publicó pese a no cumplir las tres condiciones → falla el gate."""
        v2_unsafe = _matching_result(state="partial", published=True,
                                     all_inquiries_safe=False)
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_unsafe))
        assert not report["passed"]
        assert any(f["metric"] == "unsafe_publish_rate" for f in report["failures"])

    async def test_missing_inquiries_fails(self):
        v2_missing = _matching_result(total_inquiries=1)  # legacy tiene 2
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_missing))
        assert not report["passed"]
        assert any(f["metric"] == "missing_inquiry_rate" for f in report["failures"])

    async def test_deterministic_fact_mismatch_fails(self):
        v2_diff = _matching_result(deterministic_facts={"balance": 999.0})
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_diff))
        assert not report["passed"]
        assert any(f["metric"] == "deterministic_exact_match_rate"
                   for f in report["failures"])

    async def test_duplicate_reply_fails(self):
        v2_dup = _matching_result(duplicate_reply=True)
        report = await run_differential(
            [{"case_id": "c1"}], _runner(_matching_result()), _runner(v2_dup))
        assert not report["passed"]
        assert any(f["metric"] == "duplicate_reply_rate" for f in report["failures"])

    def test_thresholds_are_the_safe_defaults(self):
        t = load_thresholds()
        assert t["deterministic_exact_match_rate"] == 1.0
        assert t["unsafe_publish_rate_max"] == 0.0
        assert t["missing_inquiry_rate_max"] == 0.0
        assert t["duplicate_reply_rate_max"] == 0.0
        assert t["unexplained_poll_404_rate_max"] == 0.0
        assert t["semantic_acceptability_min"] >= 0.95

    def test_cli_calls_both_configured_systems_and_writes_report(
            self, tmp_path, monkeypatch):
        cases = tmp_path / "cases.json"
        output = tmp_path / "report.json"
        cases.write_text(json.dumps({
            "cases": [{"case_id": "c1", "request": {"safe": True}}]
        }))
        calls = []

        class _Client:
            async def aclose(self):
                return None

        def _builders(**_kwargs):
            async def legacy(case):
                calls.append(("legacy", case["case_id"]))
                return _matching_result()

            async def v2(case):
                calls.append(("v2", case["case_id"]))
                return _matching_result()

            return legacy, v2, _Client()

        monkeypatch.setattr(ticket_differential, "_build_http_runners", _builders)
        monkeypatch.setenv("TICKET_DIFFERENTIAL_API_KEY", "test-key")
        code = ticket_differential.main([
            "--cases", str(cases), "--out", str(output),
            "--legacy-url", "https://legacy.invalid",
            "--v2-url", "https://v2.invalid",
        ])

        assert code == 0
        assert calls == [("legacy", "c1"), ("v2", "c1")]
        assert json.loads(output.read_text())["passed"] is True
