"""
Tests del fault injection SÓLO staging (plan Tarea 7 Paso 7a).

Cubre: firma HMAC válida/alterada, rechazo en producción, principal
incorrecto y cada punto de inyección.
"""

from __future__ import annotations

import json

import pytest

from data_pipeline.staging_fault_injection import (
    FAULT_TEST_HEADER,
    FaultInjectionRejected,
    InjectedFault,
    accept_fault_plan_from_request,
    build_signed_fault_plan,
    maybe_raise,
)

SECRET = "staging-only-fault-secret"


class TestFaultPlanSigning:

    def test_signed_plan_roundtrips_and_injects(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(InjectedFault) as exc:
            maybe_raise(plan, point="post_checkpoint", inquiry_index=0,
                        app_env="staging", principal_id="e2e", secret=SECRET)
        assert exc.value.point == "post_checkpoint"

    def test_tampered_signature_rejected(self):
        plan = build_signed_fault_plan(
            point="lease_lost", inquiry_index=1,
            principal_id="e2e", secret=SECRET)
        plan["signature"] = "0" * 64
        with pytest.raises(FaultInjectionRejected):
            maybe_raise(plan, point="lease_lost", inquiry_index=1,
                        app_env="staging", principal_id="e2e", secret=SECRET)

    def test_wrong_principal_rejected(self):
        plan = build_signed_fault_plan(
            point="timeout_reset", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            maybe_raise(plan, point="timeout_reset", inquiry_index=0,
                        app_env="staging", principal_id="attacker", secret=SECRET)

    def test_production_never_injects(self):
        plan = build_signed_fault_plan(
            point="dependency_down", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            maybe_raise(plan, point="dependency_down", inquiry_index=0,
                        app_env="production", principal_id="e2e", secret=SECRET)

    def test_non_matching_point_does_not_inject(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        # punto distinto: no lanza
        maybe_raise(plan, point="lease_lost", inquiry_index=0,
                    app_env="staging", principal_id="e2e", secret=SECRET)


class TestProducerHeaderAcceptance:

    def test_producer_rejects_header_in_production(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            accept_fault_plan_from_request(
                app_env="production", header_value=json.dumps(plan),
                principal_id="e2e", secret=SECRET)

    def test_producer_accepts_signed_header_in_staging(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        accepted = accept_fault_plan_from_request(
            app_env="staging", header_value=json.dumps(plan),
            principal_id="e2e", secret=SECRET)
        assert accepted["point"] == "post_checkpoint"

    def test_producer_no_header_returns_none(self):
        assert accept_fault_plan_from_request(
            app_env="staging", header_value=None,
            principal_id="e2e", secret=SECRET) is None

    def test_producer_rejects_without_secret(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            accept_fault_plan_from_request(
                app_env="staging", header_value=json.dumps(plan),
                principal_id="e2e", secret="")

    def test_header_name_is_stable(self):
        assert FAULT_TEST_HEADER == "X-ForUs-Fault-Plan"
