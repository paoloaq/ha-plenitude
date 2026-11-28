"""Gas supplies: energy profile, monthly and daily values, statistics, sensors, configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.plenitude.api import ROME, Account, GasYear, PlenitudeClient, Supply
from custom_components.plenitude.bands import PRICE_MODE
from custom_components.plenitude.const import (
    COMMODITY_GAS,
    CONF_COMMODITY,
    CONF_SUPPLY,
    DOMAIN,
    statistic_prefix,
    supply_key,
)
from custom_components.plenitude.gas import gas_series, read_months
from custom_components.plenitude.websocket import ws_info

from .conftest import POD, FakeClient, FakePortal

PDR = "99999999999999"  # fake
PREFIX = statistic_prefix(supply_key(PDR), COMMODITY_GAS)
VOLUME = f"{PREFIX}_consumo"
COST = f"{PREFIX}_costo"
CREDENTIALS = {"email": "test@example.com", "password": "secret"}

# Shapes of the capture of 2026-11-26 (values made up).
LIST = [
    {"tipo": "GAS", "pod": None, "pdr": PDR, "flagMidori": True, "status": "ATTIVO", "tipoUtente": "RETAIL"},
    {"tipo": "POWER", "pod": POD, "pdr": None, "flagMidori": True, "status": "ATTIVO", "tipoUtente": "RETAIL"},
]


def profile_entry(client_type: str, house: int) -> dict:
    # Both entries carry both codes: only clientType tells them apart.
    return {
        "clientType": client_type,
        "lightCode": POD,
        "gasCode": PDR,
        "contractId": 7,
        "houseId": house,
        "contractPower": 6 if client_type == "LIGHT" else 0,
    }


def client_answering(answers: dict[str, object]) -> tuple[PlenitudeClient, list[dict]]:
    client = PlenitudeClient(SimpleNamespace())
    asked: list[dict] = []

    async def service(method, path, *, params=None, body=None):
        asked.append(body)
        for suffix, answer in answers.items():
            if path.endswith(suffix):
                return answer(body) if callable(answer) else answer
        raise AssertionError(path)

    client._service = service
    return client, asked


@pytest.mark.parametrize("gas_first", [True, False])
async def test_energy_profile_entries_go_to_their_supply(gas_first: bool) -> None:
    entries = [profile_entry("GAS", 20), profile_entry("LIGHT", 10)]
    if not gas_first:
        entries.reverse()
    client, asked = client_answering(
        {"/lista-forniture": {"fornitura": LIST}, "/supplies": {"fornitureClienteMidori4FE": entries}}
    )
    gas, light = await client.get_supplies(Account("C", "A"))
    assert asked[1:] == [{"pod": POD, "pdr": PDR}]  # one request, as the web app
    assert gas.is_gas and not light.is_gas
    assert (light.house_id, light.contract_power) == ("10", 6.0)
    assert (gas.house_id, gas.contract_power) == ("20", None)
    assert gas.code == PDR and light.code == POD


async def test_gas_only_account_asks_with_an_empty_pod() -> None:
    client, asked = client_answering(
        {
            "/lista-forniture": {"fornitura": LIST[:1]},
            "/supplies": {"fornitureClienteMidori4FE": [profile_entry("GAS", 20)]},
        }
    )
    [gas] = await client.get_supplies(Account("C", "A"))
    assert asked[1:] == [{"pod": "", "pdr": PDR}]
    assert gas.house_id == "20"


def month_start(year: int, month: int) -> int:
    return int(datetime(year, month, 1, tzinfo=ROME).timestamp())


async def test_gas_year_view() -> None:
    names = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
    year = {
        "status": "DATA",
        "view": "YEARLY",
        "clientType": "GAS",
        "consumptionList": [],
        "additionalData": [
            {"name": n, "startTime": month_start(2026, i + 1), "endTime": 0, "consumption": 42.5 if i == 9 else 0.0}
            for i, n in enumerate(names)
        ],
    }
    client, asked = client_answering({"/analysis/consumptions": year})
    supply = Supply(kind="GAS", pod=None, pdr=PDR, contract_id="7", house_id="20")
    result = await client.get_gas_year(Account("C", "A"), supply, 2026)
    assert asked[0]["type"] == "3" and asked[0]["houseId"] == "20"
    assert asked[0]["startTime"] == "2026-01-01T00:00:00.000Z"
    assert result.months[(2026, 10)] == 42.5 and len(result.months) == 12


async def test_gas_days_view() -> None:
    supply = Supply(kind="GAS", pod=None, pdr=PDR, contract_id="7", house_id="20")
    # What the server answered on 2026-11-26: the year view, without days.
    empty = {"status": "NO_DATA", "view": "YEARLY", "consumptionList": [], "additionalData": []}
    client, _ = client_answering({"/analysis/consumptions": empty})
    assert await client.get_gas_days(Account("C", "A"), supply, 2026, 11) == {}
    # Days as for electricity.
    daily = {
        "status": "DATA",
        "view": "MONTHLY",
        "consumptionList": [
            {"activityId": 0, "data": [{"name": "01/11", "consumption": 1.5}, {"name": "02/11", "consumption": 2}]}
        ],
    }
    client, _ = client_answering({"/analysis/consumptions": daily})
    assert await client.get_gas_days(Account("C", "A"), supply, 2026, 11) == {
        date(2026, 11, 1): 1.5,
        date(2026, 11, 2): 2.0,
    }


def test_read_months_trims_the_months_without_a_reading() -> None:
    months = {(2026, m): 0.0 for m in range(1, 13)} | {(2026, 6): 3.0, (2026, 8): 5.0}
    assert read_months(months) == {(2026, 6): 3.0, (2026, 7): 0.0, (2026, 8): 5.0}
    assert read_months({(2026, 1): 0.0}) == {}


def test_series_spreads_the_month_and_prefers_real_days() -> None:
    december = date(2026, 12, 3)
    series = gas_series(
        {(2026, 10): 31.0, (2026, 11): 30.0}, {date(2026, 11, 1): 6.0, date(2026, 11, 2): 0.0}, december
    )
    october = [v for d, v in series.items() if d.month == 10]
    assert len(october) == 31 and all(v == pytest.approx(1.0) for v in october)
    # November: the real days, the rest of the month (24) over the other 28 days.
    assert series[date(2026, 11, 1)] == 6.0 and series[date(2026, 11, 2)] == 0.0
    assert series[date(2026, 11, 3)] == pytest.approx(24 / 28)
    assert sum(v for d, v in series.items() if d.month == 11) == pytest.approx(30.0)
    # Days without a monthly value yet: only the days.
    assert gas_series({}, {date(2026, 12, 1): 2.0}, december) == {date(2026, 12, 1): 2.0}


def test_series_skips_the_current_month_until_it_is_closed() -> None:
    """A value of the current month (a self-reading) covers the days up to the reading: it is not spread, and no
    row is written in the future."""
    today = date(2026, 11, 15)
    months = {(2026, 10): 31.0, (2026, 11): 15.0}
    series = gas_series(months, {}, today)
    assert max(series) == date(2026, 10, 31) and sum(series.values()) == pytest.approx(31.0)
    # Real days of the current month stay, but only those before today.
    days = {date(2026, 11, 1): 2.0, date(2026, 11, 14): 1.0, date(2026, 11, 15): 9.0}
    series = gas_series(months, days, today)
    assert {d: v for d, v in series.items() if d.month == 11} == {date(2026, 11, 1): 2.0, date(2026, 11, 14): 1.0}
    # Once the month is over, it is spread as any other.
    series = gas_series(months, {}, date(2026, 12, 1))
    assert max(series) == date(2026, 11, 30) and sum(series.values()) == pytest.approx(46.0)


@dataclass
class GasPortal(FakePortal):
    months: dict[tuple[int, int], float] = field(default_factory=dict)
    gas_days: dict[date, float] = field(default_factory=dict)
    years_asked: list[int] = field(default_factory=list)


class FakeGasClient(FakeClient):
    portal: GasPortal

    async def get_supplies(self, account) -> list[Supply]:
        return [
            *await super().get_supplies(account),
            Supply(kind="GAS", pod=None, pdr=PDR, contract_id="7", house_id="20", address="Via di prova 1"),
        ]

    async def get_gas_year(self, account, supply, year: int) -> GasYear:
        assert supply.house_id == "20"
        self.portal.years_asked.append(year)
        months = {(year, m): self.portal.months.get((year, m), 0.0) for m in range(1, 13)}
        return GasYear(year=year, status="DATA" if any(months.values()) else "NO_DATA", months=months)

    async def get_gas_days(self, account, supply, year: int, month: int) -> dict[date, float]:
        return {d: v for d, v in self.portal.gas_days.items() if (d.year, d.month) == (year, month)}


@pytest.fixture
def gas_portal():
    portal = GasPortal()
    with (
        patch("custom_components.plenitude.coordinator.PlenitudeClient", lambda session: FakeGasClient(portal)),
        patch("custom_components.plenitude.coordinator.REQUEST_PAUSE", 0),
    ):
        yield portal


async def rows(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return stats.get(statistic_id, [])


async def setup_gas(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Plenitude gas",
        unique_id=supply_key(PDR),
        data={**CREDENTIALS, CONF_SUPPLY: supply_key(PDR), CONF_COMMODITY: COMMODITY_GAS},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    return entry


def gas_sensor(hass: HomeAssistant, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{supply_key(PDR)}_{key}")
    return hass.states.get(entity_id) if entity_id else None


async def test_gas_statistics_and_sensors(recorder_mock, hass: HomeAssistant, freezer, gas_portal: GasPortal) -> None:
    freezer.move_to(datetime(2026, 11, 26, 12, tzinfo=ROME))
    gas_portal.months = {(2026, 10): 31.0}
    entry = await setup_gas(hass, {PRICE_MODE: "single", "price": 1.2})

    volume = await rows(hass, VOLUME)
    assert len(volume) == 31  # one row per day of October, at local midnight
    assert volume[0]["start"] == datetime(2026, 10, 1, tzinfo=ROME).timestamp()
    assert volume[-1]["sum"] == pytest.approx(31.0)
    assert (await rows(hass, COST))[-1]["sum"] == pytest.approx(31.0 * 1.2)
    metadata = await get_instance(hass).async_add_executor_job(lambda: get_metadata(hass, statistic_ids={VOLUME}))
    assert metadata[VOLUME][1]["unit_of_measurement"] == "m³"
    # The first import walked back until a year without consumption.
    assert gas_portal.years_asked == [2026, 2025]

    assert gas_sensor(hass, "ultimo_mese").state == "2026-10-01"
    assert float(gas_sensor(hass, "consumo_ultimo_mese").state) == pytest.approx(31.0)
    assert float(gas_sensor(hass, "costo_ultimo_mese").state) == pytest.approx(37.2)
    assert gas_sensor(hass, "ultimo_giorno") is None  # the electricity sensors are not created

    # November is read, with some real days: the statistics are rewritten, the sums stay consistent.
    gas_portal.months[(2026, 11)] = 30.0
    gas_portal.gas_days = {date(2026, 11, 1): 6.0}
    freezer.move_to(datetime(2026, 12, 3, 12, tzinfo=ROME))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    volume = await rows(hass, VOLUME)
    assert len(volume) == 61 and volume[-1]["sum"] == pytest.approx(61.0)
    assert volume[31]["state"] == pytest.approx(6.0)  # 1 November, the real value
    total = 0.0
    for row in volume:
        total += row["state"]
        assert row["sum"] == pytest.approx(total, abs=1e-6)


async def test_self_reading_of_the_current_month(
    recorder_mock, hass: HomeAssistant, freezer, gas_portal: GasPortal
) -> None:
    freezer.move_to(datetime(2026, 11, 26, 12, tzinfo=ROME))
    gas_portal.months = {(2026, 10): 31.0, (2026, 11): 15.0}  # November: a self-reading before the month ends
    entry = await setup_gas(hass, {PRICE_MODE: "none"})
    volume = await rows(hass, VOLUME)
    assert len(volume) == 31 and volume[-1]["sum"] == pytest.approx(31.0)  # October only, nothing in the future
    assert volume[-1]["start"] == datetime(2026, 10, 31, tzinfo=ROME).timestamp()
    assert gas_sensor(hass, "ultimo_mese").state == "2026-10-01"

    # The month is over and read: November is spread over its 30 days, the sums continue.
    gas_portal.months[(2026, 11)] = 30.0
    freezer.move_to(datetime(2026, 12, 3, 12, tzinfo=ROME))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    volume = await rows(hass, VOLUME)
    assert len(volume) == 61 and volume[-1]["sum"] == pytest.approx(61.0)
    assert gas_sensor(hass, "ultimo_mese").state == "2026-11-01"


async def test_gas_without_data(recorder_mock, hass: HomeAssistant, freezer, gas_portal: GasPortal) -> None:
    freezer.move_to(datetime(2026, 11, 26, 12, tzinfo=ROME))
    await setup_gas(hass, {PRICE_MODE: "none"})
    assert await rows(hass, VOLUME) == []
    assert gas_sensor(hass, "costo_ultimo_mese") is None


async def test_the_cards_do_not_see_gas(recorder_mock, hass: HomeAssistant, freezer, gas_portal: GasPortal) -> None:
    freezer.move_to(datetime(2026, 11, 26, 12, tzinfo=ROME))
    gas_portal.months = {(2026, 10): 31.0}
    await setup_gas(hass, {PRICE_MODE: "none"})
    connection = MagicMock()
    ws_info(hass, connection, {"id": 1, "type": f"{DOMAIN}/info"})
    connection.send_result.assert_called_once_with(1, {"entries": []})


def supplies() -> list[Supply]:
    return [
        Supply(kind="POWER", pod=POD, pdr=None, contract_id="1", house_id="10", address="Via di prova 1"),
        Supply(kind="GAS", pod=None, pdr=PDR, contract_id="1", house_id="20", address="Via di prova 1"),
    ]


async def test_config_flow_offers_gas(hass: HomeAssistant) -> None:
    with (
        patch("custom_components.plenitude.config_flow._usable_supplies", AsyncMock(return_value=supplies())),
        patch("custom_components.plenitude.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "supply"
        labels = [o["label"] for o in result["data_schema"].schema[CONF_SUPPLY].config["options"]]
        assert any("PDR …9999" in label for label in labels) and any("POD …9999" in label for label in labels)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SUPPLY: supply_key(PDR)})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Plenitude gas"  # one gas supply: no code in the title
    assert result["data"] == {**CREDENTIALS, CONF_SUPPLY: supply_key(PDR), CONF_COMMODITY: COMMODITY_GAS}


async def test_gas_options_have_a_single_price(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Plenitude gas",
        unique_id=supply_key(PDR),
        data={**CREDENTIALS, CONF_SUPPLY: supply_key(PDR), CONF_COMMODITY: COMMODITY_GAS},
        options={PRICE_MODE: "none"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.plenitude.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "gas"
        assert result["data_schema"].schema[PRICE_MODE].config["options"] == ["none", "single"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {PRICE_MODE: "single"})
        assert result["step_id"] == "gas_price"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"price": "1,25"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {PRICE_MODE: "single", "price": 1.25}
