"""CleverTouch climate entities"""

from typing import Optional
from datetime import timedelta

import voluptuous as vol

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    ClimateEntityDescription,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
import homeassistant.helpers.config_validation as cv

from clevertouch.devices import Radiator, HeatMode, TempType

from .const import (
    DOMAIN,
    TEMP_HA_UNIT,
    TEMP_NATIVE_UNIT,
    TEMP_NATIVE_STEP,
    TEMP_NATIVE_MIN,
    TEMP_NATIVE_MAX,
    TEMP_NATIVE_PRECISION,
)
from .coordinator import CleverTouchUpdateCoordinator, CleverTouchEntity

_DEFAULT_HEAT_PRESET: HeatMode = HeatMode.COMFORT


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CleverTouch climate entities."""
    coordinator: CleverTouchUpdateCoordinator = hass.data[DOMAIN].get(entry.entry_id)

    entities = [
        RadiatorEntity(coordinator, device)
        for home in coordinator.homes.values()
        for device in home.devices.values()
        if isinstance(device, Radiator)
    ]

    async_add_entities(entities, update_before_add=True)

    platform = async_get_current_platform()
    platform.async_register_entity_service(
        "activate_heat_mode",
        {
            vol.Required("mode"): cv.string,
            vol.Optional("temperature"): cv.positive_float,
            vol.Optional("duration"): cv.time_period,
        },
        "_async_activate_heat_mode",
    )


class RadiatorEntity(CleverTouchEntity, ClimateEntity):
    """Representation of a CleverTouch climate entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: CleverTouchUpdateCoordinator,
        radiator: Radiator,
    ) -> None:
        super().__init__(coordinator, radiator)

        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.AUTO, HVACMode.OFF]
        self._attr_preset_modes = radiator.modes
        self._last_heat_preset: HeatMode = _DEFAULT_HEAT_PRESET
        self._optimistic_mode: HeatMode | None = None
        self._radiator = radiator

        self.entity_description = ClimateEntityDescription(
            icon="mdi:radiator",
            has_entity_name=False,
            key="radiator",
        )

        self._attr_target_temperature_step = TEMP_NATIVE_STEP
        self._attr_precision = TEMP_NATIVE_PRECISION
        self._attr_temperature_unit = TEMP_HA_UNIT
        self._attr_min_temp = TEMP_NATIVE_MIN
        self._attr_max_temp = TEMP_NATIVE_MAX
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )

    def _get_heat_mode(self) -> HeatMode:
        """Return the effective heat mode (optimistic or actual)."""
        if self._optimistic_mode is not None:
            return self._optimistic_mode
        return self._radiator.heat_mode

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state and refresh _last_heat_preset from live data."""
        if self._radiator.heat_mode != HeatMode.OFF:
            self._last_heat_preset = self._radiator.heat_mode
        self._optimistic_mode = None
        super()._handle_coordinator_update()

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation ie. heat, cool, idle."""
        mode = self._get_heat_mode()
        if mode == HeatMode.OFF:
            return HVACMode.OFF
        elif mode == HeatMode.PROGRAM:
            return HVACMode.AUTO
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        mode = self._get_heat_mode()
        if mode == HeatMode.OFF:
            return HVACAction.OFF
        elif self._radiator.active:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def icon(self) -> Optional[str]:
        mode = self._get_heat_mode()
        if mode == HeatMode.OFF:
            return "mdi:radiator-off"
        elif self._radiator.active:
            return "mdi:radiator"
        return "mdi:radiator-disabled"

    @property
    def current_temperature(self) -> Optional[float]:
        temp = self._radiator.temperatures["current"].as_unit(TEMP_NATIVE_UNIT)
        if isinstance(temp, float):
            temp = round(temp, 1)
        return temp

    @property
    def target_temperature(self) -> Optional[float]:
        temp = self._radiator.temperatures["target"].as_unit(TEMP_NATIVE_UNIT)
        if isinstance(temp, float):
            temp = round(temp, 1)
        return temp

    @property
    def preset_mode(self) -> Optional[str]:
        return self._get_heat_mode()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode by mapping to the corresponding preset."""
        if hvac_mode not in self._attr_hvac_modes:
            raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")

        if hvac_mode == HVACMode.OFF:
            current = self._get_heat_mode()
            if current != HeatMode.OFF:
                self._last_heat_preset = current
            target: HeatMode = HeatMode.OFF
        elif hvac_mode == HVACMode.AUTO:
            target = HeatMode.PROGRAM
        else:  # HVACMode.HEAT
            target = self._last_heat_preset
            if target == HeatMode.OFF or target == HeatMode.PROGRAM:
                target = _DEFAULT_HEAT_PRESET

        await self._apply_heat_mode(target)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_preset_mode(self, preset_mode):
        """Set preset mode."""
        if self._radiator.heat_mode == preset_mode and self._optimistic_mode is None:
            return
        if preset_mode != HeatMode.OFF:
            self._last_heat_preset = preset_mode
        await self._apply_heat_mode(preset_mode)

    async def _apply_heat_mode(self, target) -> None:
        """Apply a heat mode optimistically, rolling back on error."""
        previous_optimistic = self._optimistic_mode
        self._optimistic_mode = target
        self.async_write_ha_state()
        try:
            await self._radiator.set_heat_mode(target)
        except Exception:
            self._optimistic_mode = previous_optimistic
            self.async_write_ha_state()
            raise
        finally:
            await self.coordinator.async_request_delayed_refresh()

    async def async_set_temperature(self, **kwargs) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if self._radiator.temp_type is None:
            return
        if self._radiator.temp_type == TempType.NONE:
            return
        if (
            self._radiator.temperatures[self._radiator.temp_type].as_unit(
                TEMP_NATIVE_UNIT
            )
            == temperature
        ):
            return
        await self._radiator.set_temperature(
            self._radiator.temp_type, temperature, TEMP_NATIVE_UNIT
        )
        await self.coordinator.async_request_delayed_refresh()

    async def _async_activate_heat_mode(
        self,
        mode: str,
        *,
        temperature: Optional[float] = None,
        duration: Optional[timedelta] = None,
    ):
        await self._radiator.activate_mode(
            mode,
            temp_value=temperature,
            temp_unit=TEMP_NATIVE_UNIT if temperature else None,
            boost_time=int(duration.total_seconds()) if duration else None,
        )
        await self.coordinator.async_request_delayed_refresh()
