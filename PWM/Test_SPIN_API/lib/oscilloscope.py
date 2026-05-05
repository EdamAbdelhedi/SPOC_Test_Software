#!/usr/bin/env python3
"""Reusable Rigol oscilloscope helper for hardware tests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pyvisa


RIGOL_INVALID_THRESHOLD = 1e37
RIGOL_VENDOR_MATCHES = ("::0X1AB1::", "::6833::")


def _safe_list(rm: pyvisa.ResourceManager, query: str) -> tuple[str, ...]:
    try:
        return rm.list_resources(query)
    except PermissionError:
        return ()


def create_resource_manager() -> pyvisa.ResourceManager:
    backends = ("@ivi", "")
    errors: list[str] = []
    for backend in backends:
        try:
            return pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
        except Exception as exc:
            label = backend or "default"
            errors.append(f"{label}: {exc}")
    try:
        return pyvisa.ResourceManager("@py")
    except Exception as exc:
        errors.append(f"@py: {exc}")
        raise RuntimeError("Unable to initialize any VISA backend: " + "; ".join(errors))


def _parse_measurement_response(response: str, label: str) -> float:
    value = float(response)
    if not math.isfinite(value) or abs(value) >= RIGOL_INVALID_THRESHOLD:
        raise ValueError(
            f"{label} is invalid on the oscilloscope (raw response: {response}). "
            "Check that the channel is connected, enabled, scaled correctly, and that a stable waveform is present."
        )
    return value


@dataclass
class OscilloscopeClient:
    resource_manager: pyvisa.ResourceManager
    resource_name: str
    timeout_ms: int = 5000

    _session: Optional[pyvisa.resources.MessageBasedResource] = None

    @classmethod
    def discover_first(cls, rm: pyvisa.ResourceManager) -> Optional[str]:
        all_resources = _safe_list(rm, "?*")
        candidates = all_resources or _safe_list(rm, "USB?*::*::INSTR")
        for resource in candidates:
            upper = resource.upper()
            if upper.startswith("USB") and any(tag in upper for tag in RIGOL_VENDOR_MATCHES):
                return resource
        return None

    def connect(self) -> pyvisa.resources.MessageBasedResource:
        if self._session is None:
            session = self.resource_manager.open_resource(self.resource_name)
            session.timeout = self.timeout_ms
            self._session = session
            self._apply_safe_defaults()
        return self._session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def query(self, command: str) -> str:
        return self.connect().query(command).strip()

    def write(self, command: str) -> None:
        self.connect().write(command)

    def _apply_safe_defaults(self) -> None:
        try:
            self.write(":STOP")
            self.write(":CLEAr")
            self.write(":ACQuire:TYPE NORMal")
        except pyvisa.VisaIOError:
            pass

    def identify(self) -> str:
        return self.query("*IDN?")

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    def measure_item(
        self,
        item: str,
        *,
        source_a: Optional[str] = None,
        source_b: Optional[str] = None,
    ) -> float:
        args = item
        if source_a:
            args = f"{args},{source_a}"
        if source_b:
            args = f"{args},{source_b}"
        response = self.query(f":MEASure:ITEM? {args}")
        return _parse_measurement_response(response, item)

    def reset_measurement_statistics(self) -> None:
        self.write(":MEASure:STATistic:RESet")

    def measure_statistic(
        self,
        item: str,
        *,
        result_type: str = "CURRent",
        source_a: Optional[str] = None,
    ) -> float:
        args = item if not source_a else f"{item},{source_a}"
        response = self.query(f":MEASure:STATistic:ITEM? {result_type},{args}")
        return _parse_measurement_response(response, f"{item} statistic {result_type}")

    def clear_measurements(self) -> None:
        self.write(":MEASure:DELete")

    def configure_channel(
        self,
        channel: int,
        *,
        channel_scale: Optional[float] = None,
        offset_volts: Optional[float] = None,
        coupling: Optional[str] = None,
        enabled: Optional[bool] = None,
        channel_probe: Optional[float] = None,
    ) -> None:
        if channel_probe is not None:
            self.write(f":CHANnel{channel}:PROBe {channel_probe}")
        if channel_scale is not None:
            self.write(f":CHANnel{channel}:SCALe {channel_scale}")
        if offset_volts is not None:
            self.write(f":CHANnel{channel}:OFFSet {offset_volts}")
        if coupling is not None:
            self.write(f":CHANnel{channel}:COUPling {coupling.upper()}")
        if enabled is not None:
            state = "ON" if enabled else "OFF"
            self.write(f":CHANnel{channel}:DISPlay {state}")

    def set_timebase(self, timebase_scale: float, delay_seconds: float = 0.0) -> None:
        self.write(f":TIMebase:SCALe {timebase_scale}")
        if abs(delay_seconds) > 0:
            self.write(":TIMebase:DELay:ENABle ON")
            self.write(f":TIMebase:DELay:OFFSet {delay_seconds}")
        else:
            self.write(":TIMebase:DELay:ENABle OFF")

    def configure_edge_trigger(
        self,
        channel: int = 1,
        *,
        trigger_edge_level: Optional[float] = None,
        slope: str = "POS",
    ) -> None:
        self.write(f":TRIGger:EDGE:SOURce CHANnel{channel}")
        if trigger_edge_level is not None:
            self.write(f":TRIGger:EDGE:LEVel {trigger_edge_level}")
        self.write(f":TRIGger:EDGE:SLOPe {slope.upper()}")


class Oscilloscope:
    """Small wrapper around the local Rigol scope client."""

    def __init__(self, resource: str | None = None, timeout_ms: int = 5000) -> None:
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._resource_manager = None
        self._client: OscilloscopeClient | None = None
        self._voltage_correction_by_channel: dict[int, float] = {}

    def connect(self) -> None:
        self._resource_manager = create_resource_manager()
        resolved = self.resource or OscilloscopeClient.discover_first(self._resource_manager)
        if not resolved:
            raise RuntimeError("No Rigol oscilloscope found. Connect it or provide a VISA resource.")
        self._client = OscilloscopeClient(self._resource_manager, resolved, timeout_ms=self.timeout_ms)
        self._client.connect()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._resource_manager is not None:
            try:
                self._resource_manager.close()
            except Exception:
                pass
            self._resource_manager = None

    def identify(self) -> str:
        return self._require_client().identify()

    def set_voltage_calibration(self, channel: int, voltage_scale_factor: float = 9.8) -> None:
        if voltage_scale_factor <= 0:
            raise ValueError("voltage_scale_factor must be > 0")
        self._voltage_correction_by_channel[int(channel)] = float(voltage_scale_factor)

    def clear_voltage_calibration(self, channel: int) -> None:
        self._voltage_correction_by_channel.pop(int(channel), None)

    def set_probe_ratio(self, channel: int, channel_probe: float = 9.8) -> None:
        if channel_probe <= 0:
            raise ValueError("channel_probe must be > 0")
        self._require_client().configure_channel(channel, channel_probe=channel_probe)

    def configure_calibrated_channel(
        self,
        channel: int,
        *,
        channel_probe: float = 9.8,
        channel_scale: float = 1.0,
        offset_volts: float = 0.0,
        coupling: str = "DC",
        enabled: bool = True,
    ) -> None:
        self.set_probe_ratio(channel, channel_probe=channel_probe)
        self.clear_voltage_calibration(channel)
        self.configure_channel(
            channel,
            channel_scale=channel_scale,
            offset_volts=offset_volts,
            coupling=coupling,
            enabled=enabled,
        )

    def get_voltage_calibration(self, channel: int) -> float:
        return self._voltage_correction_by_channel.get(int(channel), 1.0)

    def read_vmax(self, channel: int) -> float:
        return self._require_client().measure_item("VMAX", source_a=f"CHANnel{channel}")

    def read_vmax_corrected(self, channel: int) -> float:
        return self.read_vmax(channel) * self.get_voltage_calibration(channel)

    def read_vmin(self, channel: int) -> float:
        return self._require_client().measure_item("VMIN", source_a=f"CHANnel{channel}")

    def read_vmin_corrected(self, channel: int) -> float:
        return self.read_vmin(channel) * self.get_voltage_calibration(channel)

    def read_vpp(self, channel: int) -> float:
        return self._require_client().measure_item("VPP", source_a=f"CHANnel{channel}")

    def read_vpp_corrected(self, channel: int) -> float:
        return self.read_vpp(channel) * self.get_voltage_calibration(channel)

    def read_rms(self, channel: int) -> float:
        return self._require_client().measure_item("VRMS", source_a=f"CHANnel{channel}")

    def read_rms_corrected(self, channel: int) -> float:
        return self.read_rms(channel) * self.get_voltage_calibration(channel)

    def read_mean(self, channel: int) -> float:
        return self._require_client().measure_item("VAVerage", source_a=f"CHANnel{channel}")

    def read_mean_corrected(self, channel: int) -> float:
        return self.read_mean(channel) * self.get_voltage_calibration(channel)

    def read_frequency(self, channel: int) -> float:
        return self._require_client().measure_item("FREQuency", source_a=f"CHANnel{channel}")

    def read_duty_cycle(self, channel: int) -> float:
        return self._require_client().measure_item("PDUTy", source_a=f"CHANnel{channel}")

    def read_pulse_width(self, channel: int) -> float:
        return self._require_client().measure_item("PWIDth", source_a=f"CHANnel{channel}")

    def read_period(self, channel: int) -> float:
        return self._require_client().measure_item("PERiod", source_a=f"CHANnel{channel}")

    def read_rise_time(self, channel: int) -> float:
        return self._require_client().measure_item("RTIMe", source_a=f"CHANnel{channel}")

    def read_fall_time(self, channel: int) -> float:
        return self._require_client().measure_item("FTIMe", source_a=f"CHANnel{channel}")

    def read_item(self, item: str, channel: int) -> float:
        return self._require_client().measure_item(item, source_a=f"CHANnel{channel}")

    def read_delay(self, item: str, channel_a: int, channel_b: int) -> float:
        return self._require_client().measure_item(
            item,
            source_a=f"CHANnel{channel_a}",
            source_b=f"CHANnel{channel_b}",
        )

    def run(self) -> None:
        self._require_client().run()

    def stop(self) -> None:
        self._require_client().stop()

    def clear_measurements(self) -> None:
        self._require_client().clear_measurements()

    def single(self) -> None:
        self._require_client().write(":SINGle")

    def force_trigger(self) -> None:
        self._require_client().write(":TFORce")

    def autoset(self) -> None:
        self._require_client().write(":AUToset")

    def read_trigger_status(self) -> str:
        return self._require_client().query(":TRIGger:STATus?")

    def read_error(self) -> str:
        return self._require_client().query(":SYSTem:ERRor?")

    def read_statistic(
        self,
        item: str,
        channel: int,
        *,
        result_type: str = "CURRent",
    ) -> float:
        return self._require_client().measure_statistic(
            item,
            result_type=result_type,
            source_a=f"CHANnel{channel}",
        )

    def reset_statistics(self) -> None:
        self._require_client().reset_measurement_statistics()

    def read_waveform_ascii(self, channel: int, *, points: int = 1000) -> tuple[float, list[float]]:
        client = self._require_client()
        client.write(f":WAVeform:SOURce CHANnel{channel}")
        client.write(":WAVeform:MODE NORMal")
        client.write(":WAVeform:FORMat ASCii")
        client.write(f":WAVeform:POINts {points}")
        x_increment = float(client.query(":WAVeform:XINCrement?"))
        response = client.query(":WAVeform:DATA?")
        if response.startswith("#"):
            digit_count = int(response[1])
            data_start = 2 + digit_count
            response = response[data_start:]
        voltages = [float(value) for value in response.strip().split(",") if value.strip()]
        return x_increment, voltages

    def configure_channel(
        self,
        channel: int,
        *,
        channel_scale: float | None = None,
        offset_volts: float | None = None,
        coupling: str | None = None,
        enabled: bool | None = None,
        channel_probe: float | None = None,
    ) -> None:
        self._require_client().configure_channel(
            channel,
            channel_scale=channel_scale,
            offset_volts=offset_volts,
            coupling=coupling,
            enabled=enabled,
            channel_probe=channel_probe,
        )

    def set_timebase(self, timebase_scale: float, delay_seconds: float = 0.0) -> None:
        self._require_client().set_timebase(timebase_scale, delay_seconds=delay_seconds)

    def configure_edge_trigger(
        self,
        channel: int = 1,
        *,
        trigger_edge_level: float | None = None,
        slope: str = "POS",
    ) -> None:
        self._require_client().configure_edge_trigger(
            channel=channel,
            trigger_edge_level=trigger_edge_level,
            slope=slope,
        )

    def _require_client(self) -> OscilloscopeClient:
        if self._client is None:
            raise RuntimeError("Oscilloscope is not connected. Call connect() first.")
        return self._client
