#!/usr/bin/env python3
"""Reusable MUX ThingSet controller for hardware tests."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

try:
    import serial  # type: ignore
except Exception:
    print("ERROR: pyserial is required. Install with: pip install pyserial", file=sys.stderr)
    raise

try:
    from .thingset import ThingSetShell
except ImportError:
    from thingset import ThingSetShell


CHANNELS = ("ch1", "ch2", "ch3", "ch4")
MUXES = ("mux1", "mux2", "mux3")

PIN_TO_ROUTE = {
    "PA0": {"mux": "mux1", "input_index": 0},
    "PA1": {"mux": "mux1", "input_index": 1},
    "PA2": {"mux": "mux1", "input_index": 2},
    "PA3": {"mux": "mux1", "input_index": 3},
    "PA4": {"mux": "mux1", "input_index": 4},
    "PA5": {"mux": "mux1", "input_index": 5},
    "PA6": {"mux": "mux1", "input_index": 6},
    "PA7": {"mux": "mux1", "input_index": 7},
    "PA8": {"mux": "mux1", "input_index": 8},
    "PA9": {"mux": "mux1", "input_index": 9},
    "PA10": {"mux": "mux1", "input_index": 10},
    "PA13": {"mux": "mux1", "input_index": 11},
    "PA14": {"mux": "mux1", "input_index": 12},
    "PA15": {"mux": "mux1", "input_index": 13},
    "PB0": {"mux": "mux2", "input_index": 0},
    "PB1": {"mux": "mux2", "input_index": 1},
    "PB2": {"mux": "mux2", "input_index": 2},
    "PB3": {"mux": "mux2", "input_index": 3},
    "PB4": {"mux": "mux2", "input_index": 4},
    "PB5": {"mux": "mux2", "input_index": 5},
    "PB6": {"mux": "mux2", "input_index": 6},
    "PB7": {"mux": "mux2", "input_index": 7},
    "PB8": {"mux": "mux2", "input_index": 8},
    "PB9": {"mux": "mux2", "input_index": 9},
    "PB10": {"mux": "mux2", "input_index": 10},
    "PB11": {"mux": "mux2", "input_index": 11},
    "PB12": {"mux": "mux2", "input_index": 12},
    "PB13": {"mux": "mux2", "input_index": 13},
    "PB14": {"mux": "mux2", "input_index": 14},
    "PB15": {"mux": "mux2", "input_index": 15},
    "PC0": {"mux": "mux3", "input_index": 0},
    "PC1": {"mux": "mux3", "input_index": 1},
    "PC2": {"mux": "mux3", "input_index": 2},
    "PC3": {"mux": "mux3", "input_index": 3},
    "PC4": {"mux": "mux3", "input_index": 4},
    "PC5": {"mux": "mux3", "input_index": 5},
    "PC6": {"mux": "mux3", "input_index": 6},
    "PC7": {"mux": "mux3", "input_index": 7},
    "PC8": {"mux": "mux3", "input_index": 8},
    "PC9": {"mux": "mux3", "input_index": 9},
    "PC10": {"mux": "mux3", "input_index": 10},
    "PC11": {"mux": "mux3", "input_index": 11},
    "PC12": {"mux": "mux3", "input_index": 12},
    "PC13": {"mux": "mux3", "input_index": 13},
    "PD2": {"mux": "mux3", "input_index": 14},
}


@dataclass(frozen=True)
class MuxState:
    channel: str
    mux: str
    input_index: int | None
    enabled: bool | None


class MuxController:
    """Thin reusable controller for routing a CUT pin through one shield channel."""

    def __init__(self, port: str, baud: int = 115200, verbose: bool = False) -> None:
        self.port = port
        self.baud = baud
        self.verbose = verbose
        self._serial = None
        self._shell: ThingSetShell | None = None

    def connect(self) -> None:
        self._serial = serial.Serial(
            self.port,
            baudrate=self.baud,
            timeout=0.2,
            write_timeout=2.0,
            dsrdtr=False,
            rtscts=False,
            xonxoff=False,
        )
        self._shell = ThingSetShell(self._serial, verbose=self.verbose)

        last_error: Exception | None = None
        entered = False
        for attempt in range(1, 4):
            try:
                try:
                    self._serial.setDTR(False)
                    time.sleep(0.10)
                    self._serial.setDTR(True)
                except Exception:
                    pass
                try:
                    self._serial.reset_input_buffer()
                    self._serial.reset_output_buffer()
                except Exception:
                    pass
                time.sleep(1.0 if attempt == 1 else 1.5)
                entered = self._shell.enter_thingset()
                if entered:
                    break
            except serial.SerialTimeoutException as exc:
                last_error = exc
                time.sleep(0.5)

        if not entered:
            self.close()
            if last_error is not None:
                raise RuntimeError(
                    f"Write timeout while entering ThingSet on MUX ({self.port}) after 3 attempts."
                ) from last_error
            raise RuntimeError(f"Could not enter ThingSet shell on MUX ({self.port})")

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._shell = None

    def disable_all(self) -> None:
        shell = self._require_shell()
        for channel in CHANNELS:
            for mux in MUXES:
                self._set_mux_group(shell, self._mux_path(mux, channel), 0, False, required=False)

    def disable_channel(self, channel: str) -> None:
        shell = self._require_shell()
        normalized_channel = self._normalize_channel(channel)
        for mux in MUXES:
            self._set_mux_group(shell, self._mux_path(mux, normalized_channel), 0, False, required=False)

    def disable_route(self, channel: str, cut_pin: str) -> None:
        shell = self._require_shell()
        normalized_channel = self._normalize_channel(channel)
        route = self._resolve_cut_pin(cut_pin)
        self._set_mux_group(shell, self._mux_path(str(route["mux"]), normalized_channel), 0, False, required=False)

    def route(self, channel: str, cut_pin: str) -> None:
        route = self._resolve_cut_pin(cut_pin)
        self.route_raw(channel=channel, mux=route["mux"], input_index=int(route["input_index"]))

    def route_pair(self, channel_a: str, cut_pin_a: str, channel_b: str, cut_pin_b: str) -> None:
        shell = self._require_shell()
        normalized_channel_a = self._normalize_channel(channel_a)
        normalized_channel_b = self._normalize_channel(channel_b)
        route_a = self._resolve_cut_pin(cut_pin_a)
        route_b = self._resolve_cut_pin(cut_pin_b)
        mux_a = self._normalize_mux(str(route_a["mux"]))
        mux_b = self._normalize_mux(str(route_b["mux"]))
        input_index_a = int(route_a["input_index"])
        input_index_b = int(route_b["input_index"])
        self._validate_input_index(mux_a, input_index_a)
        self._validate_input_index(mux_b, input_index_b)
        self.disable_all()
        self._set_mux_group(shell, self._mux_path(mux_a, normalized_channel_a), input_index_a, True)
        self._set_mux_group(shell, self._mux_path(mux_b, normalized_channel_b), input_index_b, True)

    def route_raw(self, channel: str, mux: str, input_index: int) -> None:
        shell = self._require_shell()
        normalized_channel = self._normalize_channel(channel)
        normalized_mux = self._normalize_mux(mux)
        self._validate_input_index(normalized_mux, input_index)
        self.disable_all()
        self._set_mux_group(shell, self._mux_path(normalized_mux, normalized_channel), input_index, True)

    def route_fast(self, channel: str, cut_pin: str, previous_channel: str | None = None, previous_cut_pin: str | None = None) -> None:
        shell = self._require_shell()
        normalized_channel = self._normalize_channel(channel)
        route = self._resolve_cut_pin(cut_pin)
        normalized_mux = self._normalize_mux(str(route["mux"]))
        input_index = int(route["input_index"])
        self._validate_input_index(normalized_mux, input_index)

        if previous_channel is not None and previous_cut_pin is not None:
            prev_channel = self._normalize_channel(previous_channel)
            prev_route = self._resolve_cut_pin(previous_cut_pin)
            prev_mux = self._normalize_mux(str(prev_route["mux"]))
            if prev_channel != normalized_channel or prev_mux != normalized_mux or int(prev_route["input_index"]) != input_index:
                self._set_mux_group(shell, self._mux_path(prev_mux, prev_channel), 0, False, required=False)

        self._set_mux_group(shell, self._mux_path(normalized_mux, normalized_channel), input_index, True)

    def read_mux_state(self, channel: str, mux: str) -> MuxState:
        shell = self._require_shell()
        normalized_channel = self._normalize_channel(channel)
        normalized_mux = self._normalize_mux(mux)
        base_path = self._mux_path(normalized_mux, normalized_channel)
        channel_value = self._ts_get(shell, f"{base_path}/rChannel")
        enable_value = self._ts_get(shell, f"{base_path}/rEnable")
        return MuxState(
            channel=normalized_channel,
            mux=normalized_mux,
            input_index=self._to_int(channel_value),
            enabled=self._to_bool(enable_value),
        )

    def read_route(self, channel: str, cut_pin: str) -> MuxState:
        route = self._resolve_cut_pin(cut_pin)
        return self.read_mux_state(channel=channel, mux=str(route["mux"]))

    def _mux_path(self, mux: str, channel: str) -> str:
        return f"/Mux/{mux.capitalize()}/{channel.capitalize()}"

    def _set_mux_group(self, shell: ThingSetShell, base_path: str, input_index: int, enabled: bool, *, required: bool = True) -> bool:
        if required:
            self._ts_set(shell, f"{base_path}/wChannel", input_index)
            self._ts_set(shell, f"{base_path}/wEnable", enabled)
            self._ts_set(shell, f"{base_path}/xApply", True)
            return True

        return (
            self._ts_set_if_present(shell, f"{base_path}/wChannel", input_index)
            and self._ts_set_if_present(shell, f"{base_path}/wEnable", enabled)
            and self._ts_set_if_present(shell, f"{base_path}/xApply", True)
        )

    def _ts_set(self, shell: ThingSetShell, path: str, value: object) -> None:
        payload = str(value).lower() if isinstance(value, bool) else str(value)
        ok, response = shell.set_value(path, payload)
        if not ok:
            raise RuntimeError(f"ThingSet write failed for {path}: {response}")

    def _ts_set_if_present(self, shell: ThingSetShell, path: str, value: object) -> bool:
        payload = str(value).lower() if isinstance(value, bool) else str(value)
        ok, _response = shell.set_value(path, payload)
        return ok

    def _ts_get(self, shell: ThingSetShell, path: str) -> str | None:
        ok, value = shell.get_value(path)
        if not ok:
            raise RuntimeError(f"ThingSet read failed for {path}")
        return value

    def _normalize_channel(self, channel: str) -> str:
        normalized = channel.strip().lower()
        if normalized not in CHANNELS:
            raise ValueError(f"Unknown channel '{channel}'. Expected one of: {', '.join(CHANNELS)}")
        return normalized

    def _normalize_mux(self, mux: str) -> str:
        normalized = mux.strip().lower()
        if normalized not in MUXES:
            raise ValueError(f"Unknown mux '{mux}'. Expected one of: {', '.join(MUXES)}")
        return normalized

    def _resolve_cut_pin(self, cut_pin: str) -> dict[str, object]:
        normalized = cut_pin.strip().upper()
        if normalized not in PIN_TO_ROUTE:
            raise ValueError(f"Unknown CUT pin '{cut_pin}'.")
        return PIN_TO_ROUTE[normalized]

    def _validate_input_index(self, mux: str, input_index: int) -> None:
        max_index = 13 if mux == "mux1" else 15 if mux == "mux2" else 14
        if input_index < 0 or input_index > max_index:
            raise ValueError(f"Invalid input index {input_index} for {mux}. Expected 0..{max_index}.")

    def _to_int(self, value: str | None) -> int | None:
        if value in (None, "", "None"):
            return None
        try:
            return int(str(value), 0)
        except Exception:
            return None

    def _to_bool(self, value: str | None) -> bool | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
        return None

    def _require_shell(self) -> ThingSetShell:
        if self._shell is None:
            raise RuntimeError("MUX controller is not connected. Call connect() first.")
        return self._shell
