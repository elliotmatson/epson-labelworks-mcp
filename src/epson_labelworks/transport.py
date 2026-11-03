"""Byte transports: Bluetooth RFCOMM socket, serial (/dev/rfcommN), USB, dry-run file."""

from __future__ import annotations

import os
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class TransportError(RuntimeError):
    pass


class Transport(ABC):
    name = "base"

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    def read(self, size: int = 256, timeout: float = 0.5) -> bytes:
        """Read whatever arrives within timeout. Default: write-only transport."""
        return b""

    def describe(self) -> dict:
        return {"transport": self.name}


# --- Bluetooth Classic RFCOMM (Linux, no `rfcomm bind` needed) -------------

class RfcommSocketTransport(Transport):
    name = "rfcomm"

    def __init__(self, address: str, channel: int = 1, connect_timeout: float = 10.0,
                 attempts: int = 3):
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise TransportError("this Python build has no AF_BLUETOOTH support (Linux only)")
        self.address, self.channel = address, channel
        self.connect_timeout, self.attempts = connect_timeout, attempts
        self.sock: socket.socket | None = None

    def open(self) -> None:
        last: Exception | None = None
        for attempt in range(self.attempts):
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            s.settimeout(self.connect_timeout)
            try:
                s.connect((self.address, self.channel))
                self.sock = s
                return
            except OSError as e:
                last = e
                s.close()
                time.sleep(1.0 + attempt)
        raise TransportError(f"RFCOMM connect to {self.address} ch{self.channel} failed: {last}")

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def write(self, data: bytes) -> None:
        assert self.sock, "transport not open"
        self.sock.settimeout(30)
        self.sock.sendall(data)

    def read(self, size: int = 256, timeout: float = 0.5) -> bytes:
        assert self.sock, "transport not open"
        self.sock.settimeout(timeout)
        try:
            return self.sock.recv(size)
        except (TimeoutError, socket.timeout):
            return b""

    def describe(self) -> dict:
        return {"transport": self.name, "address": self.address, "channel": self.channel}


# --- Serial (paired + `rfcomm bind` device, or a USB-serial bridge) --------

class SerialTransport(Transport):
    name = "serial"

    def __init__(self, path: str, baud: int = 115200):
        self.path, self.baud = path, baud
        self.port = None

    def open(self) -> None:
        import serial  # pyserial

        try:
            self.port = serial.Serial(self.path, self.baud, timeout=0.5, write_timeout=30)
        except serial.SerialException as e:
            raise TransportError(f"cannot open {self.path}: {e}") from e

    def close(self) -> None:
        if self.port:
            self.port.close()
            self.port = None

    def write(self, data: bytes) -> None:
        self.port.write(data)
        self.port.flush()

    def read(self, size: int = 256, timeout: float = 0.5) -> bytes:
        self.port.timeout = timeout
        return self.port.read(size)

    def describe(self) -> dict:
        return {"transport": self.name, "path": self.path, "baud": self.baud}


# --- USB printer class (pyusb) ---------------------------------------------

EPSON_VID = 0x04B8


@dataclass
class UsbInfo:
    vid: int
    pid: int
    manufacturer: str | None
    product: str | None
    serial: str | None
    ieee1284_id: str | None
    port_status: int | None
    lwstatus_hex: str | None
    endpoints: dict


class UsbTransport(Transport):
    name = "usb"

    def __init__(self, vid: int = EPSON_VID, pid: int | None = None,
                 product_match: str | None = None):
        self.vid, self.pid, self.product_match = vid, pid, product_match
        self.dev = None
        self.ep_out = self.ep_in = None
        self._detached = False

    def _find(self):
        import usb.core
        import usb.util

        kwargs = {"idVendor": self.vid}
        if self.pid is not None:
            kwargs["idProduct"] = self.pid
        devs = list(usb.core.find(find_all=True, **kwargs))
        for d in devs:
            # printer class lives on the interface, not the device
            try:
                cfg = d.get_active_configuration()
            except Exception:
                cfg = d[0]
            intf = cfg[(0, 0)]
            if intf.bInterfaceClass != 0x07:
                continue
            if self.product_match:
                try:
                    prod = usb.util.get_string(d, d.iProduct) or ""
                except Exception:
                    prod = ""
                if self.product_match.lower() not in prod.lower():
                    continue
            return d
        raise TransportError(
            f"no Epson USB printer found (vid={self.vid:04x}"
            + (f", pid={self.pid:04x}" if self.pid else "")
            + (f", product~{self.product_match!r}" if self.product_match else "") + ")"
        )

    def open(self) -> None:
        import usb.core
        import usb.util

        self.dev = self._find()
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)  # usblp
                self._detached = True
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured
        intf = self.dev.get_active_configuration()[(0, 0)]
        usb.util.claim_interface(self.dev, intf.bInterfaceNumber)
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if self.ep_out is None:
            raise TransportError("printer interface has no bulk OUT endpoint")

    def close(self) -> None:
        if not self.dev:
            return
        import usb.util

        try:
            usb.util.release_interface(self.dev, 0)
            if self._detached:
                self.dev.attach_kernel_driver(0)
        except Exception:
            pass
        usb.util.dispose_resources(self.dev)
        self.dev = None

    def write(self, data: bytes) -> None:
        chunk = 4096
        for i in range(0, len(data), chunk):
            self.ep_out.write(data[i : i + chunk], timeout=30000)

    def read(self, size: int = 256, timeout: float = 0.5) -> bytes:
        if self.ep_in is None:
            return b""
        import usb.core

        # Read one max-size packet per transfer. The PX400 sends 64-byte status
        # frames as full packets with no terminating short packet, so a larger
        # request waits for more data and pyusb drops the partial read on timeout.
        try:
            return bytes(self.ep_in.read(self.ep_in.wMaxPacketSize, timeout=max(1, int(timeout * 1000))))
        except usb.core.USBTimeoutError:
            return b""
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 110:
                return b""
            raise

    # read-only control requests ---------------------------------------
    def get_lwstatus(self) -> bytes | None:
        """Vendor GetLWStatus: bmRequestType C1, bRequest 01, 64 bytes."""
        try:
            return bytes(self.dev.ctrl_transfer(0xC1, 0x01, 0, 0, 64, timeout=1000))
        except Exception:
            return None

    def get_device_id(self) -> str | None:
        """Printer class GET_DEVICE_ID (A1/00): 2-byte BE length + IEEE-1284 string."""
        try:
            raw = bytes(self.dev.ctrl_transfer(0xA1, 0x00, 0, 0, 1024, timeout=1000))
            n = int.from_bytes(raw[:2], "big")
            return raw[2:n].decode("ascii", errors="replace")
        except Exception:
            return None

    def get_port_status(self) -> int | None:
        try:
            return bytes(self.dev.ctrl_transfer(0xA1, 0x01, 0, 0, 1, timeout=1000))[0]
        except Exception:
            return None

    def info(self) -> UsbInfo:
        import usb.util

        def s(idx):
            try:
                return usb.util.get_string(self.dev, idx) if idx else None
            except Exception:
                return None

        lw = self.get_lwstatus()
        return UsbInfo(
            vid=self.dev.idVendor, pid=self.dev.idProduct,
            manufacturer=s(self.dev.iManufacturer), product=s(self.dev.iProduct),
            serial=s(self.dev.iSerialNumber), ieee1284_id=self.get_device_id(),
            port_status=self.get_port_status(), lwstatus_hex=lw.hex(" ") if lw else None,
            endpoints={
                "out": hex(self.ep_out.bEndpointAddress) if self.ep_out else None,
                "in": hex(self.ep_in.bEndpointAddress) if self.ep_in else None,
            },
        )

    def describe(self) -> dict:
        d = {"transport": self.name, "vid": f"{self.vid:04x}"}
        if self.pid is not None:
            d["pid"] = f"{self.pid:04x}"
        if self.product_match:
            d["product_match"] = self.product_match
        return d


# --- Dry run ---------------------------------------------------------------

class FileTransport(Transport):
    """Writes each job to <dir>/job-<timestamp>.bin. Never talks to hardware."""

    name = "file"

    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self.buf = bytearray()
        self.last_path: Path | None = None

    def open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.buf.clear()

    def close(self) -> None:
        if self.buf:
            self.last_path = self.dir / f"job-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time()*1000)%1000:03d}.bin"
            self.last_path.write_bytes(bytes(self.buf))
        self.buf.clear()

    def write(self, data: bytes) -> None:
        self.buf += data

    def describe(self) -> dict:
        return {"transport": self.name, "directory": str(self.dir)}


# --- factory -----------------------------------------------------------------

def from_env(env: dict | None = None) -> Transport:
    env = dict(os.environ if env is None else env)
    kind = env.get("LW_TRANSPORT", "file").lower()
    if kind == "rfcomm":
        addr = env.get("LW_BT_ADDRESS")
        if not addr:
            raise TransportError("LW_TRANSPORT=rfcomm needs LW_BT_ADDRESS")
        return RfcommSocketTransport(addr, int(env.get("LW_BT_CHANNEL", "1")))
    if kind == "serial":
        return SerialTransport(env.get("LW_SERIAL_PATH", "/dev/rfcomm0"),
                               int(env.get("LW_SERIAL_BAUD", "115200")))
    if kind == "usb":
        pid = env.get("LW_USB_PID")
        return UsbTransport(int(env.get("LW_USB_VID", "04b8"), 16),
                            int(pid, 16) if pid else None,
                            env.get("LW_USB_PRODUCT") or None)
    if kind == "file":
        return FileTransport(env.get("LW_DRY_RUN_DIR", "./jobs"))
    raise TransportError(f"unknown LW_TRANSPORT {kind!r} (rfcomm | serial | usb | file)")
