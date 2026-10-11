"""Charge mode selector."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import control as ctrl
from .const import (
    CHARGE_MODES,
    CONF_GRID_PHASES,
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DOMAIN,
    PHASE_1P,
    PHASE_3P,
    PHASE_PREFERENCES,
)
from .coordinator import WebastoCoordinator
from .entity import UniteEntity
from .rest_client import (
    UniteRestAuthError,
    UniteRestError,
    async_set_installation_phase,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoCoordinator = hass.data[DOMAIN][entry.entry_id]
    controller = coordinator.controller

    entities: list[SelectEntity] = [UniteModeSelect(coordinator)]
    # Only the phase select that matches the current mode/toggle is created, so
    # you never see an irrelevant (undeletable) one. Changing mode reloads the
    # entry, after which the other one is created (and the old becomes a
    # deletable orphan).
    if controller.is_external:
        entities.append(UnitePhaseSelect(coordinator))        # evcc phaseswitch
    elif controller.cfg.phase_switching:
        entities.append(UnitePhasePreferenceSelect(coordinator))  # Auto/1/3
    if (
        entry.options.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED)
        and ctrl.is_three_phase_install(
            entry.options.get(CONF_GRID_PHASES), coordinator.device.phases_supported
        )
    ):
        entities.append(UniteInstallationPhaseSelect(coordinator, entry))

    async_add_entities(entities)


class UniteModeSelect(UniteEntity, SelectEntity, RestoreEntity):
    """The live charge mode (fast / manual / solar / min_solar).

    Only meaningful with the built-in control loop; hidden under external (evcc)
    control, which owns the charging strategy.
    """

    _attr_translation_key = "mode"
    _attr_options = list(CHARGE_MODES)

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "mode")

    @property
    def available(self) -> bool:
        return super().available and not self.coordinator.controller.is_external

    @property
    def current_option(self) -> str:
        return self.coordinator.controller.mode

    async def async_select_option(self, option: str) -> None:
        if option in CHARGE_MODES:
            self.coordinator.controller.mode = option
            await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in CHARGE_MODES:
            self.coordinator.controller.mode = last.state


class UnitePhaseSelect(UniteEntity, SelectEntity):
    """Phase selector ("1"/"3") for an external controller (evcc phaseswitch).

    Writes register 405 directly. Only exposed under external control; with the
    built-in loop, phase switching is automatic.
    """

    _attr_translation_key = "phase_select"
    _attr_options = ["1", "3"]

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "phase_select")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.controller.is_external

    @property
    def current_option(self) -> str | None:
        # Report evcc's own last phase request so it does not keep re-issuing the
        # switch while a recovery pause is deliberately waiting.
        requested = self.coordinator.controller.requested_phase
        if requested in ("1", "3"):
            return requested
        data = self.coordinator.data
        if data is None or data.phase_switch_raw is None:
            return None
        return "3" if data.phase_switch_raw == 1 else "1"

    async def async_select_option(self, option: str) -> None:
        if option in ("1", "3"):
            await self.coordinator.controller.async_external_set_phase(int(option))


class UnitePhasePreferenceSelect(UniteEntity, SelectEntity, RestoreEntity):
    """Phase preference for the built-in control loop: Auto / 1 / 3.

    Auto = solar follows surplus, fast/manual use all phases. Unlike the mode,
    this preference persists when the car is unplugged and across restarts.
    Hidden under external (evcc) control.
    """

    _attr_translation_key = "phase_preference"
    _attr_options = list(PHASE_PREFERENCES)

    def __init__(self, coordinator: WebastoCoordinator) -> None:
        super().__init__(coordinator, "phase_preference")

    @property
    def available(self) -> bool:
        controller = self.coordinator.controller
        return super().available and not controller.is_external and controller.cfg.phase_switching

    @property
    def current_option(self) -> str:
        return self.coordinator.controller.phase_preference

    async def async_select_option(self, option: str) -> None:
        if option in PHASE_PREFERENCES:
            self.coordinator.controller.phase_preference = option
            await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in PHASE_PREFERENCES:
            self.coordinator.controller.phase_preference = last.state


class UniteInstallationPhaseSelect(UniteEntity, SelectEntity):
    """Change the installation phase setting directly through the web API."""

    _attr_translation_key = "installation_phase"
    _attr_options = [PHASE_1P, PHASE_3P]

    def __init__(self, coordinator: WebastoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, "installation_phase")
        self._entry = entry

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._entry.options.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED)
        )

    @property
    def current_option(self) -> str | None:
        phases = self.coordinator.device.phases_supported
        if phases is None:
            return None
        return PHASE_1P if phases == 0 else PHASE_3P

    async def async_select_option(self, option: str) -> None:
        if option not in (PHASE_1P, PHASE_3P):
            return
        try:
            await async_set_installation_phase(
                async_get_clientsession(self.hass),
                self._entry.data[CONF_HOST],
                self._entry.options.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
                self._entry.options.get(CONF_REST_PASSWORD, ""),
                0 if option == PHASE_1P else 1,
                known_fields=self.coordinator.json_config_fields,
            )
        except UniteRestAuthError as err:
            raise HomeAssistantError(f"Web UI login failed: {err}") from err
        except UniteRestError as err:
            raise HomeAssistantError(f"Could not change installation phase: {err}") from err
        controller = self.coordinator.controller
        if controller is not None:
            controller.invalidate_setpoint_cache()
        await self.coordinator.async_request_refresh()
