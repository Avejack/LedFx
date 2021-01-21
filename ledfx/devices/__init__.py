import asyncio
import logging
import socket
from abc import abstractmethod
from functools import cached_property

import numpy as np
import requests
import voluptuous as vol
import zeroconf

from ledfx.config import save_config
from ledfx.events import (  # DeviceUpdateEvent,; EffectClearedEvent,; EffectSetEvent,
    Event,
)
from ledfx.utils import BaseRegistry, RegistryLoader, generate_id

_LOGGER = logging.getLogger(__name__)


@BaseRegistry.no_registration
class Device(BaseRegistry):

    CONFIG_SCHEMA = vol.Schema(
        {
            vol.Required(
                "name", description="Friendly name for the device"
            ): str,
            vol.Optional(
                "max_brightness",
                description="Max brightness for the device",
                default=1.0,
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
            vol.Optional(
                "center_offset",
                description="Number of pixels from the perceived center of the device",
                default=0,
            ): int,
            vol.Optional(
                "refresh_rate",
                description="Maximum rate that pixels are sent to the device",
                default=60,
            ): int,
        }
    )

    _active = False
    _output_thread = None
    _active_effect = None
    _fadeout_effect = None

    def __init__(self, ledfx, config):
        self._ledfx = ledfx
        self._config = config
        # the multiplier to fade in/out of an effect. -ve values mean fading
        # in, +ve mean fading out
        self.fade_timer = 0

    def __del__(self):
        if self._active:
            self.deactivate()

    @property
    def pixel_count(self):
        pass

    def update_pixels(self, display_id, pixels, start, end):
        self._pixels[start : end + 1] = pixels
        if display_id == self.priority_display.id:
            frame = self.assemble_frame()
            self.flush(frame)

    def assemble_frame(self):
        """
        Assembles the frame to be flushed. Currently this will just return
        the active channels pixels, but will eventually handle things like
        merging multiple segments segments and alpha blending channels
        """
        frame = np.clip(
            self._pixels * self._config["max_brightness"],
            0,
            255,
        )
        if self._config["center_offset"]:
            frame = np.roll(frame, self._config["center_offset"], axis=0)
        return frame

    def activate(self):
        self._active = True
        # self._device_thread = Thread(target = self.thread_function)
        # self._device_thread.start()
        self._device_thread = None
        self.thread_function()

    def deactivate(self):
        self._active = False
        if self._device_thread:
            self._device_thread.join()
            self._device_thread = None

    @abstractmethod
    def flush(self, data):
        """
        Flushes the provided data to the device. This abstract method must be
        overwritten by the device implementation.
        """

    @property
    def name(self):
        return self._config["name"]

    @property
    def max_brightness(self):
        return self._config["max_brightness"] * 256

    @property
    def refresh_rate(self):
        return self.priority_display.refresh_rate

    @cached_property
    def priority_display(self):
        """
        Returns the first display that has the highest refresh rate of all displays
        associated with this device
        """
        refresh_rate = max(
            display.refresh_rate
            for display in self._displays
            if display.is_active
        )
        return next(
            display
            for display in self._displays
            if display.refresh_rate == refresh_rate
        )

    @cached_property
    def _displays(self):
        return [
            self._ledfx.displays.get(display["id"])
            for display in self._displays_config
        ]


class Devices(RegistryLoader):
    """Thin wrapper around the device registry that manages devices"""

    PACKAGE_NAME = "ledfx.devices"

    def __init__(self, ledfx):
        super().__init__(ledfx, Device, self.PACKAGE_NAME)

        def cleanup_effects(e):
            self.clear_all_effects()

        self._ledfx.events.add_listener(cleanup_effects, Event.LEDFX_SHUTDOWN)
        self._zeroconf = zeroconf.Zeroconf()

    def create_from_config(self, config):
        for device in config:
            _LOGGER.info("Loading device from config: {}".format(device))
            self._ledfx.devices.create(
                id=device["id"],
                type=device["type"],
                config=device["config"],
                ledfx=self._ledfx,
            )
            if "effect" in device:
                try:
                    effect = self._ledfx.effects.create(
                        ledfx=self._ledfx,
                        type=device["effect"]["type"],
                        config=device["effect"]["config"],
                    )
                    self._ledfx.devices.get_device(device["id"]).set_effect(
                        effect
                    )
                except vol.MultipleInvalid:
                    _LOGGER.warning(
                        "Effect schema changed. Not restoring effect"
                    )

    def clear_all_effects(self):
        for device in self.values():
            device.clear_frame()

    def get_device(self, device_id):
        for device in self.values():
            if device_id == device.id:
                return device
        return None

    async def find_wled_devices(self):
        # Scan the LAN network that match WLED using zeroconf - Multicast DNS
        # Service Discovery Library
        _LOGGER.info("Scanning for WLED devices...")
        wled_listener = WLEDListener(self._ledfx)
        wledbrowser = self._zeroconf.add_service_listener(
            "_wled._tcp.local.", wled_listener
        )
        try:
            await asyncio.sleep(10)
        finally:
            _LOGGER.info("Scan Finished")
            self._zeroconf.remove_service_listener(wled_listener)


class WLEDListener:
    def __init__(self, _ledfx):
        self._ledfx = _ledfx

    def remove_service(self, zeroconf_obj, type, name):
        _LOGGER.info(f"Service {name} removed")

    def add_service(self, zeroconf_obj, type, name):

        info = zeroconf_obj.get_service_info(type, name)

        if info:
            address = socket.inet_ntoa(info.addresses[0])
            hostname = str(info.server)
            url = f"http://{address}/json/info"
            # For each WLED device found, based on the WLED IPv4 address, do a
            # GET requests
            response = requests.get(url)
            b = response.json()
            # For each WLED json response, format from WLED payload to LedFx payload.
            # Note, set universe_size to 510 if LED 170 or less, If you have
            # more than 170 LED, set universe_size to 510
            wledled = b["leds"]
            wledname = b["name"]
            wledcount = wledled["count"]

            # We need to use a universe size of 510 if there are more than 170
            # pixels to prevent spanning pixel data across sequential universes
            if wledcount > 170:
                unisize = 510
            else:
                unisize = 512

            device_id = generate_id(wledname)
            device_type = "e131"
            device_config = {
                "max_brightness": 1,
                "refresh_rate": 60,
                "universe": 1,
                "universe_size": unisize,
                "name": wledname,
                "pixel_count": wledcount,
                "ip_address": hostname,
            }

            # Check this device doesn't share IP with any other device
            for device in self._ledfx.devices.values():
                if device.config["ip_address"] == hostname:
                    return

            # Create the device
            _LOGGER.info(
                "Adding device of type {} with config {}".format(
                    device_type, device_config
                )
            )
            device = self._ledfx.devices.create(
                id=device_id,
                type=device_type,
                config=device_config,
                ledfx=self._ledfx,
            )

            # Update and save the configuration
            self._ledfx.config["devices"].append(
                {
                    "id": device.id,
                    "type": device.type,
                    "config": device.config,
                }
            )
            save_config(
                config=self._ledfx.config,
                config_dir=self._ledfx.config_dir,
            )
