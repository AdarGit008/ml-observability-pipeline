"""Per-pump TLS identity via the ``{pump_id}`` placeholder (ADR 0016).

Covers ``simulator.config.tls_for_pump`` (pure expansion) and its two
call sites in ``Fleet.from_config`` — the construction loop and the
stashed ``publisher_factory`` that ``FleetExpansion``'s ``add_pump``
uses mid-run. No filesystem, no network, no monkeypatching: expansion
is string-only by design (file-existence stays in
``AwsIotPublisher.__aenter__``, per ADR 0003 §Decision 5).
"""

from __future__ import annotations

import pytest

from simulator.config import (
    BrokerConfig,
    BrokerTarget,
    FleetConfig,
    PUMP_ID_PLACEHOLDER,
    ScenarioKind,
    SimulatorConfig,
    TlsConfig,
    tls_for_pump,
)
from simulator.publisher import AwsIotPublisher, LocalPublisher
from simulator.runner import Fleet


TEMPLATED_TLS = TlsConfig(
    cert_path="simulator/.secrets/{pump_id}/{pump_id}.cert.pem",
    key_path="simulator/.secrets/{pump_id}/{pump_id}.private.key",
    ca_path="simulator/.secrets/AmazonRootCA1.pem",
)


def _config(
    *,
    target: BrokerTarget = BrokerTarget.AWS_IOT,
    tls: TlsConfig | None = TEMPLATED_TLS,
    pump_count: int = 3,
) -> SimulatorConfig:
    if target is BrokerTarget.LOCAL:
        tls = None
        url = "mqtt://localhost:1883"
    else:
        url = "example-ats.iot.eu-central-1.amazonaws.com"
    return SimulatorConfig(
        fleet=FleetConfig(
            pump_count=pump_count,
            setpoint_rpm=1800.0,
            ambient_celsius=22.0,
            base_seed=0,
        ),
        scenario=ScenarioKind.HEALTHY,
        broker=BrokerConfig(target=target, url=url, tls=tls),
        demo_mode=False,
    )


# -- tls_for_pump: pure expansion ------------------------------------------


class TestTlsForPump:
    def test_expands_cert_and_key_paths(self):
        out = tls_for_pump(TEMPLATED_TLS, "P-07")
        assert out.cert_path == "simulator/.secrets/P-07/P-07.cert.pem"
        assert out.key_path == "simulator/.secrets/P-07/P-07.private.key"

    def test_shared_ca_path_without_placeholder_unchanged(self):
        out = tls_for_pump(TEMPLATED_TLS, "P-07")
        assert out.ca_path == "simulator/.secrets/AmazonRootCA1.pem"

    def test_no_placeholder_is_identity(self):
        literal = TlsConfig(
            cert_path="certs/P-00.cert.pem",
            key_path="certs/P-00.private.key",
            ca_path="certs/AmazonRootCA1.pem",
        )
        assert tls_for_pump(literal, "P-99") == literal

    def test_multiple_occurrences_all_expanded(self):
        out = tls_for_pump(TEMPLATED_TLS, "P-03")
        assert PUMP_ID_PLACEHOLDER not in out.cert_path
        assert PUMP_ID_PLACEHOLDER not in out.key_path

    def test_input_is_untouched(self):
        before = TlsConfig(
            cert_path=TEMPLATED_TLS.cert_path,
            key_path=TEMPLATED_TLS.key_path,
            ca_path=TEMPLATED_TLS.ca_path,
        )
        tls_for_pump(TEMPLATED_TLS, "P-01")
        assert TEMPLATED_TLS == before

    def test_stray_braces_do_not_raise(self):
        # str.replace, not str.format — a path with other brace tokens
        # must pass through literally instead of raising KeyError.
        weird = TlsConfig(
            cert_path="certs/{env}/{pump_id}.cert.pem",
            key_path="certs/{env}/{pump_id}.key",
            ca_path="certs/{env}/ca.pem",
        )
        out = tls_for_pump(weird, "P-05")
        assert out.cert_path == "certs/{env}/P-05.cert.pem"
        assert out.ca_path == "certs/{env}/ca.pem"


# -- Fleet.from_config: construction loop ----------------------------------


class TestFromConfigPerPumpTls:
    def test_each_pump_gets_its_own_cert_paths(self):
        fleet = Fleet.from_config(_config(pump_count=3))
        for i, (pump, publisher) in enumerate(fleet.members):
            assert isinstance(publisher, AwsIotPublisher)
            pid = f"P-{i:02d}"
            assert pump.pump_id == pid
            assert publisher.client_id == pid
            assert publisher.tls.cert_path == (
                f"simulator/.secrets/{pid}/{pid}.cert.pem"
            )
            assert publisher.tls.key_path == (
                f"simulator/.secrets/{pid}/{pid}.private.key"
            )

    def test_tls_paths_are_distinct_across_pumps(self):
        fleet = Fleet.from_config(_config(pump_count=3))
        cert_paths = {p.tls.cert_path for _, p in fleet.members}
        key_paths = {p.tls.key_path for _, p in fleet.members}
        assert len(cert_paths) == 3
        assert len(key_paths) == 3

    def test_ca_path_is_shared_across_pumps(self):
        fleet = Fleet.from_config(_config(pump_count=3))
        ca_paths = {p.tls.ca_path for _, p in fleet.members}
        assert ca_paths == {"simulator/.secrets/AmazonRootCA1.pem"}

    def test_literal_paths_preserved_for_single_pump_smoke_config(self):
        # Pre-ADR-0016 configs (2026-05-27 smoke) name literal paths;
        # they must come through verbatim.
        literal = TlsConfig(
            cert_path="simulator/.secrets/P-00/P-00.cert.pem",
            key_path="simulator/.secrets/P-00/P-00.private.key",
            ca_path="simulator/.secrets/P-00/AmazonRootCA1.pem",
        )
        fleet = Fleet.from_config(_config(tls=literal, pump_count=1))
        ((_, publisher),) = fleet.members
        assert publisher.tls == literal

    def test_local_target_unaffected(self):
        fleet = Fleet.from_config(_config(target=BrokerTarget.LOCAL))
        for _, publisher in fleet.members:
            assert isinstance(publisher, LocalPublisher)


# -- Fleet.add_pump: the stashed publisher_factory --------------------------


class TestAddPumpPerPumpTls:
    def test_add_pump_mints_expanded_identity(self):
        # FleetExpansion grows the fleet via add_pump, which uses the
        # publisher_factory closure stashed by from_config — the new
        # pump must get ITS OWN expanded paths, not pump 0's.
        fleet = Fleet.from_config(_config(pump_count=2))
        _, publisher = fleet.add_pump("P-90")
        assert isinstance(publisher, AwsIotPublisher)
        assert publisher.client_id == "P-90"
        assert publisher.tls.cert_path == (
            "simulator/.secrets/P-90/P-90.cert.pem"
        )
        assert publisher.tls.ca_path == (
            "simulator/.secrets/AmazonRootCA1.pem"
        )

    def test_add_pump_collision_still_raises(self):
        from simulator.scenario import ScenarioError

        fleet = Fleet.from_config(_config(pump_count=2))
        with pytest.raises(ScenarioError):
            fleet.add_pump("P-01")


# -- Shared-cert misconfiguration warning ------------------------------------


class TestSharedCertWarning:
    def test_multi_pump_literal_paths_warn(self, caplog):
        literal = TlsConfig(
            cert_path="certs/one.cert.pem",
            key_path="certs/one.private.key",
            ca_path="certs/ca.pem",
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="simulator.runner"):
            Fleet.from_config(_config(tls=literal, pump_count=3))
        assert any("SAME certificate" in r.message for r in caplog.records)

    def test_placeholder_paths_do_not_warn(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="simulator.runner"):
            Fleet.from_config(_config(pump_count=3))
        assert not any("SAME certificate" in r.message for r in caplog.records)

    def test_single_pump_literal_paths_do_not_warn(self, caplog):
        # The 2026-05-27 smoke shape: one pump, literal paths — fine.
        literal = TlsConfig(
            cert_path="simulator/.secrets/P-00/P-00.cert.pem",
            key_path="simulator/.secrets/P-00/P-00.private.key",
            ca_path="simulator/.secrets/P-00/AmazonRootCA1.pem",
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="simulator.runner"):
            Fleet.from_config(_config(tls=literal, pump_count=1))
        assert not any("SAME certificate" in r.message for r in caplog.records)
