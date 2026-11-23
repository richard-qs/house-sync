from copy import deepcopy
from datetime import date

import pytest

from housesync.engine import reprice_boq
from housesync.rates import RateLibrary, RateLibraryError, load_rate_library


def surcharge(name, component, *, scope="library", values=None, kind="percentage", amount=5, end_date=None):
    return {
        "name": name, "effective_from": "2026-12-01", "end_date": end_date, "type": kind, "amount": amount,
        "component": component, "scope": {"type": scope, "values": values or []},
    }


def test_as_at_resolves_component_and_whole_rate_history():
    library = load_rate_library(pricing_data={"rates": {
        "ROOF": {
            "components": {"material": 100, "labour": 30, "plant": 5, "freight": 10},
            "versions": [
                {"effective_from": "2026-12-03", "components": {"material": 106}},
                {"effective_from": "2026-12-02", "components": {"freight": 12}},
            ],
        },
        "WALL": {"versions": [{"effective_from": "2026-12-02", "rate": 392}]},
    }})
    before = library.as_at("2026-12-01")
    on = library.as_at(date(2026, 12, 2))
    after = library.as_at("2026-12-04")
    assert [x.rows["ROOF"]["rate"] for x in (before, on, after)] == [145, 147, 153]
    assert [x.rows["WALL"]["rate"] for x in (before, on, after)] == [380, 392, 392]
    assert after.rows["ROOF"]["rate_components"] == {"material": 106, "labour": 30, "plant": 5, "freight": 12}
    assert after.rows["ROOF"]["rate_component_dates"] == {
        "material": "2026-12-03", "labour": "2026-11-17", "plant": "2026-11-17", "freight": "2026-12-02",
    }
    assert on.rows["ROOF"]["rate_changes"] == [
        {"component": "freight", "effective_from": "2026-12-02", "old_rate": 10, "new_rate": 12},
    ]
    assert RateLibrary(**after.snapshot()).as_at("2026-12-01").snapshot() == before.snapshot()
    with pytest.raises(RateLibraryError, match="calendar date"):
        library.as_at("2026-02-30")


def test_surcharges_stack_in_list_order_and_end_date_is_inclusive():
    config = {"rates": {"ROOF": {"components": {"material": 100, "labour": 30, "plant": 5, "freight": 10}}},
              "surcharges": [surcharge("Fixed haulage", "freight", kind="fixed", amount=2, end_date="2026-12-02"),
                             surcharge("Fuel levy", "freight")]}
    library = load_rate_library(pricing_data=config)
    assert library.as_at("2026-11-30").rows["ROOF"]["rate"] == 145
    on = library.as_at("2026-12-01").rows["ROOF"]
    assert on["rate"] == library.as_at("2026-12-02").rows["ROOF"]["rate"] == 147.6
    assert [(rule["name"], rule["unit_rate_change"]) for rule in on["surcharges"]] == [("Fixed haulage", 2), ("Fuel levy", 0.6)]
    assert on["rate_base_components"]["freight"] == 10
    assert on["rate_components"]["freight"] == 12.6
    assert library.as_at("2026-12-03").rows["ROOF"]["rate"] == 145.5
    assert not library.as_at("2026-12-01").rows["WALL"]["surcharges"]
    config["surcharges"].reverse()
    assert load_rate_library(pricing_data=config).as_at("2026-12-01").rows["ROOF"]["rate"] == 147.5


@pytest.mark.parametrize(("scope", "values", "expected"), [
    ("library", None, {"FLOOR", "ROOF", "WALL"}),
    ("trade", ["roofing"], {"ROOF"}),
    ("supplier", ["supplier a"], {"FLOOR", "ROOF"}),
    ("material_category", ["steel"], {"ROOF", "WALL"}),
    ("rate_codes", ["wall", "FLOOR"], {"FLOOR", "WALL"}),
])
def test_scope_filters_against_rate_metadata(scope, values, expected):
    library = load_rate_library(pricing_data={"rates": {
        "FLOOR": {"components": {"material": 95}, "supplier": "Supplier A", "material_category": "Concrete"},
        "ROOF": {"components": {"material": 145}, "supplier": "Supplier A", "material_category": "Steel"},
        "WALL": {"components": {"material": 380}, "supplier": "Supplier B", "material_category": "Steel"},
    }, "surcharges": [surcharge("Material levy", "material", scope=scope, values=values)]})
    assert {code for code, row in library.as_at("2026-12-01").rows.items() if row["surcharges"]} == expected


def test_reprice_saved_boq_keeps_geometry_overrides_and_baseline_intact():
    saved = {
        "revision": "original",
        "settings": {"rate_overrides": {"PAINT": 8}},
        "boq": [
            {"code": "ROOM1_PAINT", "rate_code": "PAINT", "group_id": "ROOM1", "gross_quantity": 24.2,
             "rate": 8, "amount": 193.6, "rate_origin": "override", "element_ids": ["W1"]},
            {"code": "ROOF", "group_id": "SHELL", "gross_quantity": 99, "rate": 145, "amount": 14355, "element_ids": ["R0"]},
        ],
        "boq_groups": [{"id": "ROOM1", "subtotal": 193.6}, {"id": "SHELL", "subtotal": 14355}],
        "totals": {"currency": "AUD", "subtotal": 14548.6},
    }
    original = deepcopy(saved)
    legacy = reprice_boq(saved, load_rate_library(), "2026-12-01")
    assert [row["amount"] for row in legacy["boq"]] == [193.6, 14355]
    assert legacy["totals"] == saved["totals"]
    library = load_rate_library(pricing_data={"surcharges": [surcharge("Levy", "base")]})
    repriced = reprice_boq(saved, library, "2026-12-01")
    assert repriced == reprice_boq(saved, library, "2026-12-01")
    assert saved == original
    assert repriced["revision"] == saved["revision"]
    assert repriced["price_as_at"] == "2026-12-01"
    paint, roof = repriced["boq"]
    assert (paint["rate"], paint["amount"], paint["surcharges"], paint["element_ids"]) == (8, 193.6, [], ["W1"])
    assert (roof["rate"], roof["amount"], roof["gross_quantity"]) == (152.25, 15072.75, 99)
    assert repriced["boq_groups"][1]["subtotal"] == 15072.75
    assert repriced["totals"]["subtotal"] == 15266.35

    # Earlier snapshots used legacy rate keys and could omit explicit provenance.
    saved["settings"] = {"rates": {"paint_m2": 8}, "rate_source": "Archived override", "rate_date": "2026-11-12"}
    saved["boq"][0]["rate_origin"] = None
    paint = reprice_boq(saved, library, "2026-12-01")["boq"][0]
    assert (paint["rate"], paint["rate_source"], paint["rate_effective_date"]) == (8, "Archived override", "2026-11-12")


def test_pricing_configuration_rejects_changes_to_legacy_baseline_and_duplicate_dates():
    with pytest.raises(RateLibraryError, match="add up to its CSV rate"):
        load_rate_library(pricing_data={"rates": {"WALL": {"components": {"material": 300}}}})
    with pytest.raises(RateLibraryError, match="multiple versions effective"):
        load_rate_library(pricing_data={"rates": {"WALL": {"versions": [
            {"effective_from": "2026-12-03", "rate": 386}, {"effective_from": "2026-12-03", "rate": 392},
        ]}}})
