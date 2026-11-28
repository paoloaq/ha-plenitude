"""Gas supplies: the consumption of the customer area as long-term statistics, one value per day.

The customer area gives gas one month at a time: the ``YEARLY`` view has one value per month (Smc), the other
views answer ``NO_DATA`` (capture of 2026-11-26). The web app also has a daily view, for the meters that send daily
readings: where it has days, they are used as they are; elsewhere the value of the month is spread evenly over its
days, so that the Energy dashboard shows the right total of every month without a spike on its first day.

Statistics (``{prefix}`` is ``plenitude:gas_<key of the supply>``), one row per day at local midnight, with a
cumulative ``sum``:

- ``{prefix}_consumo``: m³ (a Smc is counted as a m³);
- ``{prefix}_costo``: EUR, volume x (price per Smc + costs per Smc), with the percentage costs applied, when a
  price or an additional cost is configured.

The history is small (at most a few years of days): every sync that finds something new rewrites all of it, with
the sums from the first day, so that a month replaced by its real days stays consistent with the others.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import date
from functools import partial

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import ROME, GasYear, PlenitudeBadRequest, Supply, safe_error
from .bands import F1
from .const import (
    COMMODITY_GAS,
    DOMAIN,
    GAS_HISTORY_YEARS,
    GAS_RECHECK_MONTHS,
    supply_key,
)
from .coordinator import CURRENCY, PlenitudeCoordinator, PlenitudeData, _midnight_utc
from .costs import hour_price
from .texts import text

_LOGGER = logging.getLogger(__name__)

type Month = tuple[int, int]


def _month_key(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def _parse_month(key: str) -> Month:
    year, month = key.split("-")
    return int(year), int(month)


def _previous(month: Month, count: int = 1) -> Month:
    index = month[0] * 12 + month[1] - 1 - count
    return index // 12, index % 12 + 1


def _month_range(first: Month, last: Month) -> list[Month]:
    months = []
    month = first
    while month <= last:
        months.append(month)
        month = _previous(month, -1)
    return months


def _month_days(month: Month) -> list[date]:
    return [date(month[0], month[1], day) for day in range(1, calendar.monthrange(*month)[1] + 1)]


def read_months(months: Mapping[Month, float]) -> dict[Month, float]:
    """The months with a reading: from the first to the last month with consumption.

    The year view gives 0 also for the months before the supply started and for those not read yet; a 0 between two
    months with consumption is a real 0 (a summer without heating).
    """
    used = sorted(month for month, value in months.items() if value)
    if not used:
        return {}
    return {month: months.get(month, 0.0) for month in _month_range(used[0], used[-1])}


def gas_series(months: Mapping[Month, float], days: Mapping[date, float], today: date) -> dict[date, float]:
    """Consumption per day: the days given by the customer area; for a month without them, its value spread evenly.

    A month with some days only: the rest of its value, if any, is spread over the days without a value.

    Only the months closed before ``today`` are spread: a value of the current month (a self-reading, for example)
    covers the days up to the reading, not the whole month, and spreading it would write rows in the future. The
    month is spread when it is read after its end; until then only its real days before ``today`` are used.
    """
    series: dict[date, float] = {}
    current = (today.year, today.month)
    read = read_months({month: value for month, value in months.items() if month < current})
    days = {day: value for day, value in days.items() if day < today}
    for month in sorted(set(read) | {(day.year, day.month) for day in days}):
        month_days = _month_days(month)
        real = {day: days[day] for day in month_days if day in days}
        series.update(real)
        if month not in read:
            continue
        missing = [day for day in month_days if day not in real]
        rest = read[month] - sum(real.values())
        if missing and (rest > 0 or not real):
            share = max(rest, 0.0) / len(missing)
            series.update({day: share for day in missing})
    return series


class GasCoordinator(PlenitudeCoordinator):
    """One gas supply of one Plenitude account."""

    commodity = COMMODITY_GAS

    def _is_mine(self, supply: Supply, wanted: str) -> bool:
        return supply.is_gas and bool(supply.pdr) and supply_key(supply.pdr or "") == wanted

    @property
    def smc_price(self) -> float:
        """EUR per Smc as in the cost statistic: price, costs per Smc and percentages (no bands for gas)."""
        return hour_price(F1, self.config_entry.options, self.cost_items)

    def _metadata(self, statistic_id: str) -> StatisticMetaData:
        lang = self.language
        if statistic_id == self.cost_id:
            label, unit, unit_class = text(lang, "cost"), CURRENCY, None
        else:
            label, unit, unit_class = text(lang, "energy"), UnitOfVolume.CUBIC_METERS, VolumeConverter.UNIT_CLASS
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{self.config_entry.title} {label}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )

    # --------------------------------------------------------------------- data

    def _stored_months(self) -> dict[Month, float]:
        return {_parse_month(key): float(value) for key, value in (self._state.get("gas_months") or {}).items()}

    def _stored_days(self) -> dict[date, float]:
        return {date.fromisoformat(key): float(value) for key, value in (self._state.get("gas_days") or {}).items()}

    async def _get_years(self, years: list[int]) -> list[GasYear]:
        assert self._account and self.supply
        account, supply = self._account, self.supply
        answers = await self._in_rounds([partial(self.client.get_gas_year, account, supply, y) for y in years])
        results: list[GasYear] = []
        for year, answer in zip(years, answers, strict=True):
            if isinstance(answer, PlenitudeBadRequest):
                self._note_refused(str(year), answer)
                continue
            results.append(answer)
        return results

    async def _get_gas_days(self, months: list[Month]) -> dict[Month, dict[date, float]]:
        assert self._account and self.supply
        account, supply = self._account, self.supply
        answers = await self._in_rounds([partial(self.client.get_gas_days, account, supply, *m) for m in months])
        results: dict[Month, dict[date, float]] = {}
        for month, answer in zip(months, answers, strict=True):
            if isinstance(answer, PlenitudeBadRequest):
                self._note_refused(_month_key(month), answer)
                continue
            results[month] = answer
        return results

    async def _sync(self) -> PlenitudeData:
        today = dt_util.now(ROME).date()
        current: Month = (today.year, today.month)
        first_import = "gas_months" not in self._state
        months = self._stored_months()
        days = self._stored_days()
        if not first_import:
            # New prices or costs: rewritten from the stored values at once, before asking the customer area, which
            # may not answer (as for electricity since 0.5.1). Nothing is written when nothing changed.
            await self._write(gas_series(months, days, today))
        await self._ensure_supply(today)

        if first_import:
            # Walk back one year at a time until a year without consumption (the current one may have none yet).
            fetched: list[GasYear] = []
            for year in range(today.year, today.year - GAS_HISTORY_YEARS, -1):
                answer = await self._get_years([year])
                fetched += answer
                if year < today.year and not any(v for a in answer for v in a.months.values()):
                    break
            if not fetched and self._refusal is not None:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="communication_error",
                    translation_placeholders={"error": safe_error(self._refusal)},
                ) from self._refusal
        else:
            years = sorted({today.year, _previous(current, GAS_RECHECK_MONTHS)[0]})
            fetched = await self._get_years(years)
        for answer in fetched:
            for month, value in answer.months.items():
                if month <= current:
                    months[month] = value

        # Daily values: the whole history on the first import, the last months afterwards.
        read = read_months(months)
        if first_import:
            ask = _month_range(min(read), current) if read else [current]
        else:
            ask = _month_range(_previous(current, GAS_RECHECK_MONTHS), current)
        for month, values in (await self._get_gas_days(ask)).items():
            for day in _month_days(month):
                days.pop(day, None)
            days.update({day: value for day, value in values.items() if day < today})

        self._state["gas_months"] = {_month_key(m): v for m, v in sorted(months.items())}
        self._state["gas_days"] = {d.isoformat(): v for d, v in sorted(days.items())}
        series = gas_series(months, days, today)
        await self._write(series)
        self._state["gaps_checked"] = today.isoformat()
        await self._save()
        return self._gas_summary(series)

    async def _write(self, series: dict[date, float]) -> None:
        """Rewrite the statistics when the series or the prices changed since the last write."""
        signature = self._cost_signature()
        digest = hashlib.sha256(
            json.dumps([[d.isoformat(), round(v, 6)] for d, v in sorted(series.items())]).encode()
        ).hexdigest()[:16]
        written = f"{digest}|{signature}"
        if self._state.get("gas_written") == written:
            return
        first = min(series) if series else None
        previous_first = self._state.get("first_day")
        if not series or (previous_first and first and first.isoformat() != previous_first):
            # Rows before the new first day would keep sums that do not follow the new ones.
            self._clear([self.energy_id, self.cost_id])
        if not self.has_cost:
            self._clear([self.cost_id])
        if series:
            price = self.smc_price
            energy: list[StatisticData] = []
            cost: list[StatisticData] = []
            total = total_cost = 0.0
            for day, value in sorted(series.items()):
                # The sums add up the rounded values: a spread month has many periodic decimals.
                moment = _midnight_utc(day)
                volume, money = round(value, 6), round(value * price, 6)
                total, total_cost = total + volume, total_cost + money
                energy.append(StatisticData(start=moment, state=volume, sum=round(total, 6)))
                cost.append(StatisticData(start=moment, state=money, sum=round(total_cost, 6)))
            async_add_external_statistics(self.hass, self._metadata(self.energy_id), energy)
            if self.has_cost:
                async_add_external_statistics(self.hass, self._metadata(self.cost_id), cost)
            self._state["first_day"] = first.isoformat() if first else None
            _LOGGER.debug("Plenitude gas: %d days written from %s", len(series), first)
        else:
            self._state.pop("first_day", None)
            _LOGGER.info("Plenitude gas: no consumption data available yet")
        self._state["gas_written"] = written
        self._state["price_signature"] = signature
        await self._written()

    def _gas_summary(self, series: dict[date, float]) -> PlenitudeData:
        """The last day and the last month with consumption."""
        if not series:
            return PlenitudeData()
        last_day = max(series)
        month = (last_day.year, last_day.month)
        volume = sum(value for day, value in series.items() if (day.year, day.month) == month)
        return PlenitudeData(
            last_day=last_day,
            last_day_kwh=round(series[last_day], 3),
            last_day_cost=round(series[last_day] * self.smc_price, 2) if self.has_cost else None,
            last_month=date(*month, 1),
            last_month_volume=round(volume, 3),
            last_month_cost=round(volume * self.smc_price, 2) if self.has_cost else None,
        )

    async def _summary(self) -> PlenitudeData:
        today = dt_util.now(ROME).date()
        return self._gas_summary(gas_series(self._stored_months(), self._stored_days(), today))
