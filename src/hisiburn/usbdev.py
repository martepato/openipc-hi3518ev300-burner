"""libusb plumbing: finding the camera and moving bytes over its bulk pipes.

On macOS this needs no driver installation at all. The camera's interface is
vendor-specific (``bInterfaceClass = 0xFF``), so no kernel extension claims
it, and libusb can open it directly — which is the entire reason the Windows
workflow needs Zadig to swap in libusbK and this one needs nothing.
"""

from __future__ import annotations

import logging
import platform
import time
from dataclasses import dataclass

import usb.core
import usb.util

log = logging.getLogger(__name__)

#: Huawei's vendor id; HiSilicon is a Huawei subsidiary and uses it throughout.
VENDOR_ID = 0x12D1

#: The one product id in play. Both stages present it: the boot ROM waiting for
#: a download and the U-Boot burn agent it starts are indistinguishable by
#: descriptor — captures of both show byte-identical device and configuration
#: descriptors. Which one is listening has to be established by asking.
PRODUCT_ID = 0xD001

KNOWN_PRODUCT_IDS = (PRODUCT_ID,)

DEFAULT_TIMEOUT_MS = 5000


class BackendMissing(Exception):
    """libusb itself is not installed, so no USB call can be made at all."""

    HINT = (
        "pyusb cannot find libusb.\n"
        "  macOS:  brew install libusb\n"
        "  Debian/Ubuntu:  sudo apt install libusb-1.0-0\n"
        "  Fedora:  sudo dnf install libusb1\n"
        "This is a missing library on your machine, not a problem with the camera."
    )

    def __init__(self, detail: str = ""):
        super().__init__(f"{self.HINT}\n({detail})" if detail else self.HINT)


class DeviceNotFound(Exception):
    """No camera in a USB download mode is attached."""


class UsbError(Exception):
    """A transfer failed or the device answered with something unusable."""


@dataclass(frozen=True)
class FoundDevice:
    """A candidate device and enough context to explain it to the user."""

    vendor_id: int
    product_id: int
    bus: int | None
    address: int | None
    manufacturer: str | None
    product: str | None

    @property
    def role(self) -> str:
        if self.product_id == PRODUCT_ID:
            return "HiUSBBurn (boot ROM or burn agent — ask it to tell them apart)"
        return "unrecognised HiSilicon device"

    @property
    def location(self) -> tuple[int | None, int | None]:
        """Bus and address, which change when the device re-enumerates."""
        return (self.bus, self.address)

    def __str__(self) -> str:
        location = ""
        if self.bus is not None and self.address is not None:
            location = f" at bus {self.bus} device {self.address}"
        label = " ".join(part for part in (self.manufacturer, self.product) if part)
        label = f" [{label}]" if label else ""
        return f"{self.vendor_id:04x}:{self.product_id:04x}{location}{label} — {self.role}"


def _string_or_none(device: usb.core.Device, index: str) -> str | None:
    """Read a descriptor string, tolerating devices that refuse the request."""
    try:
        return getattr(device, index)
    except (usb.core.USBError, ValueError, NotImplementedError):
        return None


def _describe(device: usb.core.Device) -> FoundDevice:
    return FoundDevice(
        vendor_id=device.idVendor,
        product_id=device.idProduct,
        bus=getattr(device, "bus", None),
        address=getattr(device, "address", None),
        manufacturer=_string_or_none(device, "manufacturer"),
        product=_string_or_none(device, "product"),
    )


def _find(**kwargs: object) -> object:
    """``usb.core.find`` with the no-libusb case turned into a useful message."""
    try:
        return usb.core.find(**kwargs)
    except usb.core.NoBackendError as exc:
        raise BackendMissing(str(exc)) from exc


def list_devices(vendor_id: int = VENDOR_ID) -> list[FoundDevice]:
    """Every attached device under ``vendor_id``, whether or not we know it."""
    return [_describe(dev) for dev in _find(find_all=True, idVendor=vendor_id)]


def find_device(
    product_id: int | None = None,
    exclude: tuple[int | None, int | None] | None = None,
) -> usb.core.Device:
    """Return the raw pyusb handle for an attached camera.

    ``exclude`` skips a device at a given (bus, address), which is how the
    handoff after stage 1 is detected: the same product id comes back at a new
    address once the loaded U-Boot re-enumerates.
    """
    wanted = (product_id,) if product_id is not None else KNOWN_PRODUCT_IDS
    for pid in wanted:
        for device in _find(find_all=True, idVendor=VENDOR_ID, idProduct=pid):
            if exclude is not None and (
                getattr(device, "bus", None),
                getattr(device, "address", None),
            ) == exclude:
                continue
            return device
    raise DeviceNotFound(
        "no HiSilicon device in USB download mode found "
        f"(looked for {', '.join(f'{VENDOR_ID:04x}:{pid:04x}' for pid in wanted)}). "
        "Unplug the camera, hold its reset button, plug it back in while still holding."
    )


def wait_for_device(
    product_id: int | None = None,
    timeout: float = 30.0,
    poll_interval: float = 0.2,
    exclude: tuple[int | None, int | None] | None = None,
) -> usb.core.Device:
    """Poll until the camera shows up, or give up after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    last_error: DeviceNotFound | None = None
    while time.monotonic() < deadline:
        try:
            return find_device(product_id, exclude=exclude)
        except DeviceNotFound as exc:
            last_error = exc
            time.sleep(poll_interval)
    raise last_error or DeviceNotFound("timed out waiting for device")


class BulkPipe:
    """An opened camera, reduced to a read/write pair over its bulk endpoints.

    Endpoint addresses are read from the descriptors rather than assumed, so a
    firmware that numbers them differently still works.
    """

    def __init__(self, device: usb.core.Device, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.device = device
        self.timeout_ms = timeout_ms
        self.info = _describe(device)
        self._interface_number: int | None = None
        self._claimed = False
        self._open()

    def _open(self) -> None:
        if platform.system() == "Linux":
            # macOS has no kernel driver on a vendor-specific interface to detach.
            try:
                if self.device.is_kernel_driver_active(0):
                    self.device.detach_kernel_driver(0)
            except (usb.core.USBError, NotImplementedError) as exc:
                log.debug("kernel driver detach skipped: %s", exc)

        try:
            self.device.set_configuration()
        except usb.core.USBError as exc:
            # Already configured is fine; anything else is a real failure.
            if exc.errno not in (16, None):  # EBUSY
                raise UsbError(f"could not configure device: {exc}") from exc
            log.debug("set_configuration skipped: %s", exc)

        config = self.device.get_active_configuration()
        interface = config[(0, 0)]
        self._interface_number = interface.bInterfaceNumber

        self.ep_out = usb.util.find_descriptor(
            interface,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
        )
        self.ep_in = usb.util.find_descriptor(
            interface,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
        )
        if self.ep_out is None or self.ep_in is None:
            raise UsbError(
                "device does not expose the expected bulk IN/OUT endpoint pair; "
                "run `hisiburn probe --verbose` and file the output as an issue"
            )

        try:
            usb.util.claim_interface(self.device, self._interface_number)
            self._claimed = True
        except usb.core.USBError as exc:
            raise UsbError(
                f"could not claim interface {self._interface_number}: {exc}. "
                "Another program may hold the device; on Linux try sudo."
            ) from exc

        log.debug(
            "opened %s: OUT 0x%02x IN 0x%02x maxpacket %d",
            self.info,
            self.ep_out.bEndpointAddress,
            self.ep_in.bEndpointAddress,
            self.ep_out.wMaxPacketSize,
        )

    @property
    def max_packet_size(self) -> int:
        return int(self.ep_out.wMaxPacketSize)

    def clear_halt(self) -> None:
        """Clear a stall on both bulk endpoints.

        A device that does not recognise a frame can stall its endpoint, and
        on macOS the stall persists: every later transfer fails with EIO until
        it is cleared. Without this, one unrecognised opcode kills the pipe and
        every subsequent operation reports a misleading I/O error.
        """
        for endpoint in (self.ep_out, self.ep_in):
            try:
                self.device.clear_halt(endpoint.bEndpointAddress)
            except usb.core.USBError as exc:
                log.debug("clear_halt on 0x%02x failed: %s", endpoint.bEndpointAddress, exc)

    def write(self, data: bytes, timeout_ms: int | None = None) -> int:
        """Send ``data`` on the bulk OUT endpoint."""
        try:
            return self.ep_out.write(data, timeout_ms or self.timeout_ms)
        except usb.core.USBError as exc:
            self.clear_halt()
            raise UsbError(f"bulk write of {len(data)} bytes failed: {exc}") from exc

    def read(self, length: int | None = None, timeout_ms: int | None = None) -> bytes:
        """Read up to ``length`` bytes (default: one max-size packet)."""
        size = length or self.max_packet_size
        try:
            return bytes(self.ep_in.read(size, timeout_ms or self.timeout_ms))
        except usb.core.USBError as exc:
            self.clear_halt()
            raise UsbError(f"bulk read failed: {exc}") from exc

    def read_byte(self, timeout_ms: int | None = None) -> int:
        """Read a single status byte, ignoring any NUL padding after it.

        The agent sends ``strlen(s) + 1`` bytes, so a bare ACK arrives as
        ``AA 00``; taking only the first byte keeps the pipe aligned.
        """
        data = self.read(self.max_packet_size, timeout_ms)
        if not data:
            raise UsbError("device returned an empty packet where a status byte was expected")
        return data[0]

    def close(self) -> None:
        if self._claimed and self._interface_number is not None:
            try:
                usb.util.release_interface(self.device, self._interface_number)
            except usb.core.USBError as exc:
                log.debug("release_interface failed: %s", exc)
            self._claimed = False
        usb.util.dispose_resources(self.device)

    def __enter__(self) -> BulkPipe:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
