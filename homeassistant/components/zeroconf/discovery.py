"""Zeroconf discovery for Home Assistant."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from fnmatch import translate
from functools import lru_cache, partial
from ipaddress import IPv4Address, IPv6Address
import logging
import re
from typing import TYPE_CHECKING, Any, Final, cast

from zeroconf import BadTypeInNameException, IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.discovery_flow import DiscoveryKey
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.service_info.zeroconf import (
    ZeroconfServiceInfo as _ZeroconfServiceInfo,
)
from homeassistant.loader import HomeKitDiscoveredIntegration, ZeroconfMatcher
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, REQUEST_TIMEOUT

if TYPE_CHECKING:
    from .models import HaZeroconf

_LOGGER = logging.getLogger(__name__)

ZEROCONF_TYPE = "_home-assistant._tcp.local."
HOMEKIT_TYPES = [
    "_hap._tcp.local.",
    # Thread based devices
    "_hap._udp.local.",
]
_HOMEKIT_MODEL_SPLITS = (None, " ", "-")


HOMEKIT_PAIRED_STATUS_FLAG = "sf"
HOMEKIT_MODEL_LOWER = "md"
HOMEKIT_MODEL_UPPER = "MD"

ATTR_DOMAIN: Final = "domain"
ATTR_NAME: Final = "name"
ATTR_PROPERTIES: Final = "properties"


DATA_DISCOVERY: HassKey[ZeroconfDiscovery] = HassKey("zeroconf_discovery")


def build_homekit_model_lookups(
    homekit_models: dict[str, HomeKitDiscoveredIntegration],
) -> tuple[
    dict[str, HomeKitDiscoveredIntegration],
    dict[re.Pattern, HomeKitDiscoveredIntegration],
]:
    """Build lookups for homekit models."""
    homekit_model_lookup: dict[str, HomeKitDiscoveredIntegration] = {}
    homekit_model_matchers: dict[re.Pattern, HomeKitDiscoveredIntegration] = {}

    for model, discovery in homekit_models.items():
        if "*" in model or "?" in model or "[" in model:
            homekit_model_matchers[_compile_fnmatch(model)] = discovery
        else:
            homekit_model_lookup[model] = discovery

    return homekit_model_lookup, homekit_model_matchers


@lru_cache(maxsize=4096, typed=True)
def _compile_fnmatch(pattern: str) -> re.Pattern:
    """Compile a fnmatch pattern."""
    return re.compile(translate(pattern))


@lru_cache(maxsize=1024, typed=True)
def _memorized_fnmatch(name: str, pattern: str) -> bool:
    """Memorized version of fnmatch that has a larger lru_cache.

    The default version of fnmatch only has a lru_cache of 256 entries.
    With many devices we quickly reach that limit and end up compiling
    the same pattern over and over again.

    Zeroconf has its own memorized fnmatch with its own lru_cache
    since the data is going to be relatively the same
    since the devices will not change frequently
    """
    return bool(_compile_fnmatch(pattern).match(name))


def _match_against_props(matcher: dict[str, str], props: dict[str, str | None]) -> bool:
    """Check a matcher to ensure all values in props."""
    for key, value in matcher.items():
        prop_val = props.get(key)
        if prop_val is None or not _memorized_fnmatch(prop_val.lower(), value):
            return False
    return True


def is_homekit_paired(props: dict[str, Any]) -> bool:
    """Check properties to see if a device is homekit paired."""
    if HOMEKIT_PAIRED_STATUS_FLAG not in props:
        return False
    with contextlib.suppress(ValueError):
        # 0 means paired and not discoverable by iOS clients)
        return int(props[HOMEKIT_PAIRED_STATUS_FLAG]) == 0
    # If we cannot tell, we assume its not paired
    return False


def async_get_homekit_discovery(
    homekit_model_lookups: dict[str, HomeKitDiscoveredIntegration],
    homekit_model_matchers: dict[re.Pattern, HomeKitDiscoveredIntegration],
    props: dict[str, Any],
) -> HomeKitDiscoveredIntegration | None:
    """Handle a HomeKit discovery.

    Return the domain to forward the discovery data to
    """
    if not (
        model := props.get(HOMEKIT_MODEL_LOWER) or props.get(HOMEKIT_MODEL_UPPER)
    ) or not isinstance(model, str):
        return None

    for split_str in _HOMEKIT_MODEL_SPLITS:
        key = (model.split(split_str))[0] if split_str else model
        if discovery := homekit_model_lookups.get(key):
            return discovery

    for pattern, discovery in homekit_model_matchers.items():
        if pattern.match(model):
            return discovery

    return None


def info_from_service(service: AsyncServiceInfo) -> _ZeroconfServiceInfo | None:
    """Return prepared info from mDNS entries."""
    # See https://ietf.org/rfc/rfc6763.html#section-6.4 and
    # https://ietf.org/rfc/rfc6763.html#section-6.5 for expected encodings
    # for property keys and values
    if not (maybe_ip_addresses := service.ip_addresses_by_version(IPVersion.All)):
        return None
    if TYPE_CHECKING:
        ip_addresses = cast(list[IPv4Address | IPv6Address], maybe_ip_addresses)
    else:
        ip_addresses = maybe_ip_addresses
    ip_address: IPv4Address | IPv6Address | None = None
    for ip_addr in ip_addresses:
        if not ip_addr.is_link_local and not ip_addr.is_unspecified:
            ip_address = ip_addr
            break
    if not ip_address:
        return None

    if TYPE_CHECKING:
        assert service.server is not None, (
            "server cannot be none if there are addresses"
        )
    return _ZeroconfServiceInfo(
        ip_address=ip_address,
        ip_addresses=ip_addresses,
        port=service.port,
        hostname=service.server,
        type=service.type,
        name=service.name,
        properties=service.decoded_properties,
    )


class ZeroconfDiscovery:
    """Discovery via zeroconf."""

    def __init__(
        self,
        hass: HomeAssistant,
        zeroconf: HaZeroconf,
        zeroconf_types: dict[str, list[ZeroconfMatcher]],
        homekit_model_lookups: dict[str, HomeKitDiscoveredIntegration],
        homekit_model_matchers: dict[re.Pattern, HomeKitDiscoveredIntegration],
    ) -> None:
        """Init discovery."""
        self.hass = hass
        self.zeroconf = zeroconf
        self.zeroconf_types = zeroconf_types
        self.homekit_model_lookups = homekit_model_lookups
        self.homekit_model_matchers = homekit_model_matchers
        self.async_service_browser: AsyncServiceBrowser | None = None
        self._service_update_listeners: set[Callable[[AsyncServiceInfo], None]] = set()
        self._service_removed_listeners: set[Callable[[str], None]] = set()

    @callback
    def async_register_service_update_listener(
        self,
        listener: Callable[[AsyncServiceInfo], None],
    ) -> Callable[[], None]:
        """Register a service update listener."""
        self._service_update_listeners.add(listener)
        return partial(self._service_update_listeners.remove, listener)

    @callback
    def async_register_service_removed_listener(
        self,
        listener: Callable[[str], None],
    ) -> Callable[[], None]:
        """Register a service removed listener."""
        self._service_removed_listeners.add(listener)
        return partial(self._service_removed_listeners.remove, listener)

    async def async_setup(self) -> None:
        """Start discovery."""
        types = list(self.zeroconf_types)
        # We want to make sure we know about other HomeAssistant
        # instances as soon as possible to avoid name conflicts
        # so we always browse for ZEROCONF_TYPE
        types.extend(
            hk_type
            for hk_type in (ZEROCONF_TYPE, *HOMEKIT_TYPES)
            if hk_type not in self.zeroconf_types
        )
        _LOGGER.debug("Starting Zeroconf browser for: %s", types)
        self.async_service_browser = AsyncServiceBrowser(
            self.zeroconf, types, handlers=[self.async_service_update]
        )

        async_dispatcher_connect(
            self.hass,
            config_entries.signal_discovered_config_entry_removed(DOMAIN),
            self._handle_config_entry_removed,
        )

    async def async_stop(self) -> None:
        """Cancel the service browser and stop processing the queue."""
        if self.async_service_browser:
            await self.async_service_browser.async_cancel()

    @callback
    def _handle_config_entry_removed(
        self,
        entry: config_entries.ConfigEntry,
    ) -> None:
        """Handle config entry changes."""
        for discovery_key in entry.discovery_keys[DOMAIN]:
            if discovery_key.version != 1:
                continue
            _type = discovery_key.key[0]
            name = discovery_key.key[1]
            _LOGGER.debug("Rediscover service %s.%s", _type, name)
            self._async_service_update(self.zeroconf, _type, name)

    def _async_dismiss_discoveries(self, name: str) -> None:
        """Dismiss all discoveries for the given name."""
        for flow in self.hass.config_entries.flow.async_progress_by_init_data_type(
            _ZeroconfServiceInfo,
            lambda service_info: bool(service_info.name == name),
        ):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])

    @callback
    def async_service_update(
        self,
        zeroconf: HaZeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Service state changed."""
        _LOGGER.debug(
            "service_update: type=%s name=%s state_change=%s",
            service_type,
            name,
            state_change,
        )

        if state_change is ServiceStateChange.Removed:
            self._async_dismiss_discoveries(name)
            for listener in self._service_removed_listeners:
                listener(name)
            return

        self._async_service_update(zeroconf, service_type, name)

    @callback
    def _async_service_update(
        self,
        zeroconf: HaZeroconf,
        service_type: str,
        name: str,
    ) -> None:
        """Service state added or changed."""
        try:
            async_service_info = AsyncServiceInfo(service_type, name)
        except BadTypeInNameException as ex:
            # Some devices broadcast a name that is not a valid DNS name
            # This is a bug in the device firmware and we should ignore it
            _LOGGER.debug("Bad name in zeroconf record: %s: %s", name, ex)
            return

        if async_service_info.load_from_cache(zeroconf):
            self._async_process_service_update(async_service_info, service_type, name)
        else:
            self.hass.async_create_background_task(
                self._async_lookup_and_process_service_update(
                    zeroconf, async_service_info, service_type, name
                ),
                name=f"zeroconf lookup {name}.{service_type}",
            )

    async def _async_lookup_and_process_service_update(
        self,
        zeroconf: HaZeroconf,
        async_service_info: AsyncServiceInfo,
        service_type: str,
        name: str,
    ) -> None:
        """Update and process a zeroconf update."""
        await async_service_info.async_request(zeroconf, REQUEST_TIMEOUT)
        self._async_process_service_update(async_service_info, service_type, name)

    @callback
    def _async_process_service_update(
        self, async_service_info: AsyncServiceInfo, service_type: str, name: str
    ) -> None:
        """Process a zeroconf update."""
        for listener in self._service_update_listeners:
            listener(async_service_info)
        info = info_from_service(async_service_info)
        if not info:
            # Prevent the browser thread from collapsing
            _LOGGER.debug("Failed to get addresses for device %s", name)
            return
        _LOGGER.debug("Discovered new device %s %s", name, info)
        props: dict[str, str | None] = info.properties
        discovery_key = DiscoveryKey(
            domain=DOMAIN,
            key=(info.type, info.name),
            version=1,
        )
        domain = None

        # If we can handle it as a HomeKit discovery, we do that here.
        if service_type in HOMEKIT_TYPES and (
            homekit_discovery := async_get_homekit_discovery(
                self.homekit_model_lookups, self.homekit_model_matchers, props
            )
        ):
            domain = homekit_discovery.domain
            discovery_flow.async_create_flow(
                self.hass,
                homekit_discovery.domain,
                {"source": config_entries.SOURCE_HOMEKIT},
                info,
                discovery_key=discovery_key,
            )
            # Continue on here as homekit_controller
            # still needs to get updates on devices
            # so it can see when the 'c#' field is updated.
            #
            # We only send updates to homekit_controller
            # if the device is already paired in order to avoid
            # offering a second discovery for the same device
            if not is_homekit_paired(props) and not homekit_discovery.always_discover:
                # If the device is paired with HomeKit we must send on
                # the update to homekit_controller so it can see when
                # the 'c#' field is updated. This is used to detect
                # when the device has been reset or updated.
                #
                # If the device is not paired and we should not always
                # discover it, we can stop here.
                return

        if not (matchers := self.zeroconf_types.get(service_type)):
            return

        # Not all homekit types are currently used for discovery
        # so not all service type exist in zeroconf_types
        for matcher in matchers:
            if len(matcher) > 1:
                if ATTR_NAME in matcher and not _memorized_fnmatch(
                    info.name.lower(), matcher[ATTR_NAME]
                ):
                    continue
                if ATTR_PROPERTIES in matcher and not _match_against_props(
                    matcher[ATTR_PROPERTIES], props
                ):
                    continue

            matcher_domain = matcher[ATTR_DOMAIN]
            # Create a type annotated regular dict since this is a hot path and creating
            # a regular dict is slightly cheaper than calling ConfigFlowContext
            context: config_entries.ConfigFlowContext = {
                "source": config_entries.SOURCE_ZEROCONF,
            }
            if domain:
                # Domain of integration that offers alternative API to handle
                # this device.
                context["alternative_domain"] = domain

            discovery_flow.async_create_flow(
                self.hass,
                matcher_domain,
                context,
                info,
                discovery_key=discovery_key,
            )
