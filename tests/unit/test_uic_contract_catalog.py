"""UIC contract catalog ↔ registry alignment."""

from app.services.missions.uic_contract_catalog import validate_uic_catalog_against_registry


def test_catalog_matches_registry_capabilities():
    errs = validate_uic_catalog_against_registry()
    assert not errs, ";\n".join(errs)
