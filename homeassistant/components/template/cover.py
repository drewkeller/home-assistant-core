"""Support for covers which integrate with other components."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    DEVICE_CLASSES_SCHEMA,
    ENTITY_ID_FORMAT,
    PLATFORM_SCHEMA as COVER_PLATFORM_SCHEMA,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import (
    CONF_COVERS,
    CONF_DEVICE_CLASS,
    CONF_ENTITY_ID,
    CONF_FRIENDLY_NAME,
    CONF_NAME,
    CONF_OPTIMISTIC,
    CONF_STATE,
    CONF_UNIQUE_ID,
    CONF_VALUE_TEMPLATE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import config_validation as cv, template
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_OBJECT_ID, CONF_PICTURE, DOMAIN
from .template_entity import (
    LEGACY_FIELDS as TEMPLATE_ENTITY_LEGACY_FIELDS,
    TEMPLATE_ENTITY_AVAILABILITY_SCHEMA,
    TEMPLATE_ENTITY_COMMON_SCHEMA_LEGACY,
    TEMPLATE_ENTITY_ICON_SCHEMA,
    TemplateEntity,
    rewrite_common_legacy_to_modern_conf,
)

_LOGGER = logging.getLogger(__name__)

OPEN_STATE = "open"
OPENING_STATE = "opening"
CLOSED_STATE = "closed"
CLOSING_STATE = "closing"

_VALID_STATES = [
    OPEN_STATE,
    OPENING_STATE,
    CLOSED_STATE,
    CLOSING_STATE,
    "true",
    "false",
    "none",
]

CONF_POSITION = "position"
CONF_POSITION_TEMPLATE = "position_template"
CONF_TILT = "tilt"
CONF_TILT_TEMPLATE = "tilt_template"
OPEN_ACTION = "open_cover"
CLOSE_ACTION = "close_cover"
STOP_ACTION = "stop_cover"
POSITION_ACTION = "set_cover_position"
TILT_ACTION = "set_cover_tilt_position"
CONF_TILT_OPTIMISTIC = "tilt_optimistic"

CONF_OPEN_AND_CLOSE = "open_or_close"

TILT_FEATURES = (
    CoverEntityFeature.OPEN_TILT
    | CoverEntityFeature.CLOSE_TILT
    | CoverEntityFeature.STOP_TILT
    | CoverEntityFeature.SET_TILT_POSITION
)

LEGACY_FIELDS = TEMPLATE_ENTITY_LEGACY_FIELDS | {
    CONF_VALUE_TEMPLATE: CONF_STATE,
    CONF_POSITION_TEMPLATE: CONF_POSITION,
    CONF_TILT_TEMPLATE: CONF_TILT,
}

DEFAULT_NAME = "Template Cover"

COVER_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Inclusive(CLOSE_ACTION, CONF_OPEN_AND_CLOSE): cv.SCRIPT_SCHEMA,
            vol.Inclusive(OPEN_ACTION, CONF_OPEN_AND_CLOSE): cv.SCRIPT_SCHEMA,
            vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
            vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.template,
            vol.Optional(CONF_OPTIMISTIC): cv.boolean,
            vol.Optional(CONF_PICTURE): cv.template,
            vol.Optional(CONF_POSITION): cv.template,
            vol.Optional(CONF_STATE): cv.template,
            vol.Optional(CONF_TILT_OPTIMISTIC): cv.boolean,
            vol.Optional(CONF_TILT): cv.template,
            vol.Optional(CONF_UNIQUE_ID): cv.string,
            vol.Optional(POSITION_ACTION): cv.SCRIPT_SCHEMA,
            vol.Optional(STOP_ACTION): cv.SCRIPT_SCHEMA,
            vol.Optional(TILT_ACTION): cv.SCRIPT_SCHEMA,
        }
    )
    .extend(TEMPLATE_ENTITY_AVAILABILITY_SCHEMA.schema)
    .extend(TEMPLATE_ENTITY_ICON_SCHEMA.schema),
    cv.has_at_least_one_key(OPEN_ACTION, POSITION_ACTION),
)

LEGACY_COVER_SCHEMA = vol.All(
    cv.deprecated(CONF_ENTITY_ID),
    vol.Schema(
        {
            vol.Inclusive(OPEN_ACTION, CONF_OPEN_AND_CLOSE): cv.SCRIPT_SCHEMA,
            vol.Inclusive(CLOSE_ACTION, CONF_OPEN_AND_CLOSE): cv.SCRIPT_SCHEMA,
            vol.Optional(STOP_ACTION): cv.SCRIPT_SCHEMA,
            vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
            vol.Optional(CONF_POSITION_TEMPLATE): cv.template,
            vol.Optional(CONF_TILT_TEMPLATE): cv.template,
            vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
            vol.Optional(CONF_OPTIMISTIC): cv.boolean,
            vol.Optional(CONF_TILT_OPTIMISTIC): cv.boolean,
            vol.Optional(POSITION_ACTION): cv.SCRIPT_SCHEMA,
            vol.Optional(TILT_ACTION): cv.SCRIPT_SCHEMA,
            vol.Optional(CONF_FRIENDLY_NAME): cv.string,
            vol.Optional(CONF_ENTITY_ID): cv.entity_ids,
            vol.Optional(CONF_UNIQUE_ID): cv.string,
        }
    ).extend(TEMPLATE_ENTITY_COMMON_SCHEMA_LEGACY.schema),
    cv.has_at_least_one_key(OPEN_ACTION, POSITION_ACTION),
)

PLATFORM_SCHEMA = COVER_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_COVERS): cv.schema_with_slug_keys(LEGACY_COVER_SCHEMA)}
)


def rewrite_legacy_to_modern_conf(
    hass: HomeAssistant, config: dict[str, dict]
) -> list[dict]:
    """Rewrite legacy switch configuration definitions to modern ones."""
    covers = []

    for object_id, entity_conf in config.items():
        entity_conf = {**entity_conf, CONF_OBJECT_ID: object_id}

        entity_conf = rewrite_common_legacy_to_modern_conf(
            hass, entity_conf, LEGACY_FIELDS
        )

        if CONF_NAME not in entity_conf:
            entity_conf[CONF_NAME] = template.Template(object_id, hass)

        covers.append(entity_conf)

    return covers


@callback
def _async_create_template_tracking_entities(
    async_add_entities: AddEntitiesCallback,
    hass: HomeAssistant,
    definitions: list[dict],
    unique_id_prefix: str | None,
) -> None:
    """Create the template switches."""
    covers = []

    for entity_conf in definitions:
        unique_id = entity_conf.get(CONF_UNIQUE_ID)

        if unique_id and unique_id_prefix:
            unique_id = f"{unique_id_prefix}-{unique_id}"

        covers.append(
            CoverTemplate(
                hass,
                entity_conf,
                unique_id,
            )
        )

    async_add_entities(covers)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Template cover."""
    if discovery_info is None:
        _async_create_template_tracking_entities(
            async_add_entities,
            hass,
            rewrite_legacy_to_modern_conf(hass, config[CONF_COVERS]),
            None,
        )
        return

    _async_create_template_tracking_entities(
        async_add_entities,
        hass,
        discovery_info["entities"],
        discovery_info["unique_id"],
    )


class CoverTemplate(TemplateEntity, CoverEntity):
    """Representation of a Template cover."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
        unique_id,
    ) -> None:
        """Initialize the Template cover."""
        super().__init__(hass, config=config, fallback_name=None, unique_id=unique_id)
        if (object_id := config.get(CONF_OBJECT_ID)) is not None:
            self.entity_id = async_generate_entity_id(
                ENTITY_ID_FORMAT, object_id, hass=hass
            )
        name = self._attr_name
        if TYPE_CHECKING:
            assert name is not None
        self._template = config.get(CONF_STATE)

        self._position_template = config.get(CONF_POSITION)
        self._tilt_template = config.get(CONF_TILT)
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)

        # The config requires (open and close scripts) or a set position script,
        # therefore the base supported features will always include them.
        self._attr_supported_features = (
            CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        )
        for action_id, supported_feature in (
            (OPEN_ACTION, 0),
            (CLOSE_ACTION, 0),
            (STOP_ACTION, CoverEntityFeature.STOP),
            (POSITION_ACTION, CoverEntityFeature.SET_POSITION),
            (TILT_ACTION, TILT_FEATURES),
        ):
            # Scripts can be an empty list, therefore we need to check for None
            if (action_config := config.get(action_id)) is not None:
                self.add_script(action_id, action_config, name, DOMAIN)
                self._attr_supported_features |= supported_feature

        optimistic = config.get(CONF_OPTIMISTIC)
        self._optimistic = optimistic or (
            optimistic is None and not self._template and not self._position_template
        )
        tilt_optimistic = config.get(CONF_TILT_OPTIMISTIC)
        self._tilt_optimistic = tilt_optimistic or not self._tilt_template
        self._position: int | None = None
        self._is_opening = False
        self._is_closing = False
        self._tilt_value: int | None = None

    @callback
    def _async_setup_templates(self) -> None:
        """Set up templates."""
        if self._template:
            self.add_template_attribute(
                "_position", self._template, None, self._update_state
            )
        if self._position_template:
            self.add_template_attribute(
                "_position",
                self._position_template,
                None,
                self._update_position,
                none_on_template_error=True,
            )
        if self._tilt_template:
            self.add_template_attribute(
                "_tilt_value",
                self._tilt_template,
                None,
                self._update_tilt,
                none_on_template_error=True,
            )
        super()._async_setup_templates()

    @callback
    def _update_state(self, result):
        super()._update_state(result)
        if isinstance(result, TemplateError):
            self._position = None
            return

        state = str(result).lower()

        if state in _VALID_STATES:
            if not self._position_template:
                if state in ("true", OPEN_STATE):
                    self._position = 100
                else:
                    self._position = 0

            self._is_opening = state == OPENING_STATE
            self._is_closing = state == CLOSING_STATE
        else:
            _LOGGER.error(
                "Received invalid cover is_on state: %s for entity %s. Expected: %s",
                state,
                self.entity_id,
                ", ".join(_VALID_STATES),
            )
            if not self._position_template:
                self._position = None

            self._is_opening = False
            self._is_closing = False

    @callback
    def _update_position(self, result):
        if result is None:
            self._position = None
            return

        try:
            state = float(result)
        except ValueError as err:
            _LOGGER.error(err)
            self._position = None
            return

        if state < 0 or state > 100:
            self._position = None
            _LOGGER.error(
                "Cover position value must be between 0 and 100. Value was: %.2f",
                state,
            )
        else:
            self._position = state

    @callback
    def _update_tilt(self, result):
        if result is None:
            self._tilt_value = None
            return

        try:
            state = float(result)
        except ValueError as err:
            _LOGGER.error(err)
            self._tilt_value = None
            return

        if state < 0 or state > 100:
            self._tilt_value = None
            _LOGGER.error(
                "Tilt value must be between 0 and 100. Value was: %.2f",
                state,
            )
        else:
            self._tilt_value = state

    @property
    def is_closed(self) -> bool | None:
        """Return if the cover is closed."""
        if self._position is None:
            return None

        return self._position == 0

    @property
    def is_opening(self) -> bool:
        """Return if the cover is currently opening."""
        return self._is_opening

    @property
    def is_closing(self) -> bool:
        """Return if the cover is currently closing."""
        return self._is_closing

    @property
    def current_cover_position(self) -> int | None:
        """Return current position of cover.

        None is unknown, 0 is closed, 100 is fully open.
        """
        if self._position_template or self._action_scripts.get(POSITION_ACTION):
            return self._position
        return None

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return current position of cover tilt.

        None is unknown, 0 is closed, 100 is fully open.
        """
        return self._tilt_value

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Move the cover up."""
        if open_script := self._action_scripts.get(OPEN_ACTION):
            await self.async_run_script(open_script, context=self._context)
        elif position_script := self._action_scripts.get(POSITION_ACTION):
            await self.async_run_script(
                position_script,
                run_variables={"position": 100},
                context=self._context,
            )
        if self._optimistic:
            self._position = 100
            self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Move the cover down."""
        if close_script := self._action_scripts.get(CLOSE_ACTION):
            await self.async_run_script(close_script, context=self._context)
        elif position_script := self._action_scripts.get(POSITION_ACTION):
            await self.async_run_script(
                position_script,
                run_variables={"position": 0},
                context=self._context,
            )
        if self._optimistic:
            self._position = 0
            self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Fire the stop action."""
        if stop_script := self._action_scripts.get(STOP_ACTION):
            await self.async_run_script(stop_script, context=self._context)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set cover position."""
        self._position = kwargs[ATTR_POSITION]
        await self.async_run_script(
            self._action_scripts[POSITION_ACTION],
            run_variables={"position": self._position},
            context=self._context,
        )
        if self._optimistic:
            self.async_write_ha_state()

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Tilt the cover open."""
        self._tilt_value = 100
        await self.async_run_script(
            self._action_scripts[TILT_ACTION],
            run_variables={"tilt": self._tilt_value},
            context=self._context,
        )
        if self._tilt_optimistic:
            self.async_write_ha_state()

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Tilt the cover closed."""
        self._tilt_value = 0
        await self.async_run_script(
            self._action_scripts[TILT_ACTION],
            run_variables={"tilt": self._tilt_value},
            context=self._context,
        )
        if self._tilt_optimistic:
            self.async_write_ha_state()

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the cover tilt to a specific position."""
        self._tilt_value = kwargs[ATTR_TILT_POSITION]
        await self.async_run_script(
            self._action_scripts[TILT_ACTION],
            run_variables={"tilt": self._tilt_value},
            context=self._context,
        )
        if self._tilt_optimistic:
            self.async_write_ha_state()
