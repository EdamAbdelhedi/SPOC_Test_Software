#!/usr/bin/env python3
"""Reusable CUT PWM ThingSet controller for hardware tests."""

from __future__ import annotations

import math
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


CUT_PWM_BASE = "/Cut/PwmOut"
SPIN_PWM_BASE = "/Spin/Pwm"

# Keep these action ids aligned with spin_pwm_action_t in Firmware/CUT/src/spin_data_objects.h.
PWM_ACT_INIT_BURST = 23
PWM_ACT_SET_BURST = 24
PWM_ACT_START_BURST = 25
PWM_ACT_STOP_BURST = 26
PWM_ACT_DEINIT_BURST = 27

LFT_ALIGNED = 0
UPDWN = 16
PWMX1 = 0
PWMX2 = 1
DEFAULT_PWM_FREQUENCY_HZ = 100000
DEFAULT_PWM_MODULATION = UPDWN
DEFAULT_SWITCH_CONVENTION = PWMX1
DUTY_MIN = 0.0
DUTY_MAX = 1.0
PHASE_MIN_DEG = -180
PHASE_MAX_DEG = 180
PWM_UNITS = ("A", "B", "C", "D", "E", "F")
PWM_FIELDS = {
    "A": {"out1": "wA1", "out2": "wA2", "duty": "wADuty", "phase": "wAPhase_deg", "dead_rise": "wADeadRise", "dead_fall": "wADeadFall", "mod": "wAMod", "switch": "wASwitchConv", "period": "rAPeriod"},
    "B": {"out1": "wB1", "out2": None, "duty": "wBDuty", "phase": "wBPhase_deg", "dead_rise": "wBDeadRise", "dead_fall": "wBDeadFall", "mod": "wBMod", "switch": "wBSwitchConv", "period": "rBPeriod"},
    "C": {"out1": "wC1", "out2": "wC2", "duty": "wCDuty", "phase": "wCPhase_deg", "dead_rise": "wCDeadRise", "dead_fall": "wCDeadFall", "mod": "wCMod", "switch": "wCSwitchConv", "period": "rCPeriod"},
    "D": {"out1": "wD1", "out2": "wD2", "duty": "wDDuty", "phase": "wDPhase_deg", "dead_rise": "wDDeadRise", "dead_fall": "wDDeadFall", "mod": "wDMod", "switch": "wDSwitchConv", "period": "rDPeriod"},
    "E": {"out1": "wE1", "out2": "wE2", "duty": "wEDuty", "phase": "wEPhase_deg", "dead_rise": "wEDeadRise", "dead_fall": "wEDeadFall", "mod": "wEMod", "switch": "wESwitchConv", "period": "rEPeriod"},
    "F": {"out1": "wF1", "out2": "wF2", "duty": "wFDuty", "phase": "wFPhase_deg", "dead_rise": "wFDeadRise", "dead_fall": "wFDeadFall", "mod": "wFMod", "switch": "wFSwitchConv", "period": "rFPeriod"},
}
PWM_RESOLUTION_FIELD_CANDIDATES = (
    "r{unit}ResolutionPs",
    "r{unit}ResolutionPS",
    "r{unit}Resolution_ps",
    "r{unit}ResPs",
    "r{unit}Res_ps",
)
PWM_SOURCES = {
    "PA8": {"unit": "A", "output": 1, "tu": 0},
    "PA9": {"unit": "A", "output": 2, "tu": 0},
    "PA10": {"unit": "B", "output": 1, "tu": 1},
    "PB12": {"unit": "C", "output": 1, "tu": 2},
    "PB13": {"unit": "C", "output": 2, "tu": 2},
    "PB14": {"unit": "D", "output": 1, "tu": 3},
    "PB15": {"unit": "D", "output": 2, "tu": 3},
    "PC8": {"unit": "E", "output": 1, "tu": 4},
    "PC9": {"unit": "E", "output": 2, "tu": 4},
    "PC6": {"unit": "F", "output": 1, "tu": 5},
    "PC7": {"unit": "F", "output": 2, "tu": 5},
}


@dataclass(frozen=True)
class CutPwmState:
    enabled: bool | None
    enabled_outputs: tuple[str, ...]
    status: int | None


@dataclass(frozen=True)
class PwmCommandValues:
    freq_hz: int
    min_freq_hz: int
    duty: float
    phase_deg: int
    dead_rise_ns: int
    dead_fall_ns: int
    modulation: int
    switch_convention: int


def clamp_pwm_duty(duty: float) -> float:
    if not math.isfinite(duty):
        raise ValueError("duty must be a finite value")
    return min(max(duty, DUTY_MIN), DUTY_MAX)


def is_pwm_pin_complementary(pin: str) -> bool:
    normalized = pin.strip().upper()
    if normalized not in PWM_SOURCES:
        raise ValueError(f"Unknown PWM-capable CUT pin '{pin}'.")
    return int(PWM_SOURCES[normalized]["output"]) == 2


def expected_pwm_pin_duty_percent(pin: str, command_duty: float) -> float:
    primary_duty = clamp_pwm_duty(command_duty) * 100.0
    if is_pwm_pin_complementary(pin):
        return 100.0 - primary_duty
    return primary_duty


def resolve_switch_convention(convention: int | str | None) -> int:
    if convention is None:
        return DEFAULT_SWITCH_CONVENTION
    if isinstance(convention, str):
        normalized = convention.strip().upper()
        if normalized in {"PWMX1", "PWM1", "0"}:
            return PWMX1
        if normalized in {"PWMX2", "PWM2", "1"}:
            return PWMX2
        raise ValueError("switch_convention must be PWMx1 or PWMx2")
    value = int(convention)
    if value not in (PWMX1, PWMX2):
        raise ValueError("switch_convention must be 0 (PWMx1) or 1 (PWMx2)")
    return value


def clamp_pwm_phase_deg(phase_deg: int | float) -> int:
    if not math.isfinite(float(phase_deg)):
        raise ValueError("phase_deg must be a finite value")
    phase_int = int(phase_deg)
    return min(max(phase_int, PHASE_MIN_DEG), PHASE_MAX_DEG)


def resolve_pwm_frequency_hz(freq_hz: int) -> int:
    if freq_hz < 0:
        raise ValueError("freq_hz must be >= 0")
    if freq_hz == 0:
        return DEFAULT_PWM_FREQUENCY_HZ
    return freq_hz


def resolve_pwm_min_frequency_hz(min_freq_hz: int | None, freq_hz: int) -> int:
    if min_freq_hz is None:
        return freq_hz
    if min_freq_hz < 0:
        raise ValueError("min_freq_hz must be >= 0")
    if min_freq_hz == 0 or min_freq_hz > freq_hz:
        return freq_hz
    return min_freq_hz


class CutPwmController:
    """Reusable CUT PWM controller for square-wave hardware tests."""

    def __init__(self, port: str, baud: int = 115200, verbose: bool = False) -> None:
        self.port = port
        self.baud = baud
        self.verbose = verbose
        self._serial = None
        self._shell: ThingSetShell | None = None
        self._resolution_field_by_unit: dict[str, str | None] = {}

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
                    f"Write timeout while entering ThingSet on CUT ({self.port}) after 3 attempts."
                ) from last_error
            raise RuntimeError(f"Could not enter ThingSet shell on CUT ({self.port})")

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._shell = None

    def set_pwm(
        self,
        pin: str,
        freq_hz: int,
        duty: float,
        enable: bool = True,
        *,
        min_freq_hz: int | None = None,
        phase_deg: int = 0,
        dead_rise_ns: int = 0,
        dead_fall_ns: int = 0,
        modulation: int | None = None,
        switch_convention: int | str | None = None,
    ) -> PwmCommandValues:
        target = self._resolve_pin(pin)
        return self.set_pwm_raw(
            unit=str(target["unit"]),
            output=int(target["output"]),
            freq_hz=freq_hz,
            duty=duty,
            enable=enable,
            min_freq_hz=min_freq_hz,
            phase_deg=phase_deg,
            dead_rise_ns=dead_rise_ns,
            dead_fall_ns=dead_fall_ns,
            modulation=modulation,
            switch_convention=switch_convention,
        )

    def set_pwm_raw(
        self,
        *,
        unit: str,
        output: int,
        freq_hz: int,
        duty: float,
        enable: bool = True,
        min_freq_hz: int | None = None,
        phase_deg: int = 0,
        dead_rise_ns: int = 0,
        dead_fall_ns: int = 0,
        modulation: int | None = None,
        switch_convention: int | str | None = None,
    ) -> PwmCommandValues:
        shell = self._require_shell()
        command = self._prepare_pwm_args(
            freq_hz=freq_hz,
            duty=duty,
            min_freq_hz=min_freq_hz,
            phase_deg=phase_deg,
            dead_rise_ns=dead_rise_ns,
            dead_fall_ns=dead_fall_ns,
            modulation=modulation,
            switch_convention=switch_convention,
        )
        resolved_unit, resolved_output = self._resolve_unit_output(unit, output)
        plan = self._empty_pwm_plan()
        unit_plan = plan[resolved_unit]
        unit_plan["duty"] = command.duty
        unit_plan["phase_deg"] = command.phase_deg
        unit_plan["dead_rise_ns"] = command.dead_rise_ns
        unit_plan["dead_fall_ns"] = command.dead_fall_ns
        unit_plan["switch_convention"] = command.switch_convention
        unit_plan[f"out{resolved_output}"] = True
        self._apply_pwm_plan(shell, plan, command, enable)
        return command

    def set_pwm_pair(
        self,
        pin_a: str,
        pin_b: str,
        freq_hz: int,
        duty: float,
        enable: bool = True,
        *,
        min_freq_hz: int | None = None,
        phase_deg: int = 0,
        dead_rise_ns: int = 0,
        dead_fall_ns: int = 0,
        modulation: int | None = None,
        switch_convention: int | str | None = None,
    ) -> PwmCommandValues:
        first = self._resolve_pin(pin_a)
        second = self._resolve_pin(pin_b)
        return self.set_pwm_pair_raw(
            unit_a=str(first["unit"]),
            output_a=int(first["output"]),
            unit_b=str(second["unit"]),
            output_b=int(second["output"]),
            freq_hz=freq_hz,
            duty=duty,
            enable=enable,
            min_freq_hz=min_freq_hz,
            phase_deg=phase_deg,
            dead_rise_ns=dead_rise_ns,
            dead_fall_ns=dead_fall_ns,
            modulation=modulation,
            switch_convention=switch_convention,
        )

    def set_pwm_pair_raw(
        self,
        *,
        unit_a: str,
        output_a: int,
        unit_b: str,
        output_b: int,
        freq_hz: int,
        duty: float,
        enable: bool = True,
        min_freq_hz: int | None = None,
        phase_deg: int = 0,
        dead_rise_ns: int = 0,
        dead_fall_ns: int = 0,
        modulation: int | None = None,
        switch_convention: int | str | None = None,
    ) -> PwmCommandValues:
        shell = self._require_shell()
        command = self._prepare_pwm_args(
            freq_hz=freq_hz,
            duty=duty,
            min_freq_hz=min_freq_hz,
            phase_deg=phase_deg,
            dead_rise_ns=dead_rise_ns,
            dead_fall_ns=dead_fall_ns,
            modulation=modulation,
            switch_convention=switch_convention,
        )
        resolved_unit_a, resolved_output_a = self._resolve_unit_output(unit_a, output_a)
        resolved_unit_b, resolved_output_b = self._resolve_unit_output(unit_b, output_b)
        if resolved_unit_a == resolved_unit_b and resolved_output_a == resolved_output_b:
            raise ValueError("primary and secondary PWM outputs must be different")
        if resolved_unit_a == resolved_unit_b and command.phase_deg != 0:
            raise ValueError("phase shift between two outputs of the same PWM unit must be 0")

        plan = self._empty_pwm_plan()
        unit_a_plan = plan[resolved_unit_a]
        unit_a_plan["duty"] = command.duty
        unit_a_plan["phase_deg"] = 0
        unit_a_plan["dead_rise_ns"] = command.dead_rise_ns
        unit_a_plan["dead_fall_ns"] = command.dead_fall_ns
        unit_a_plan["switch_convention"] = command.switch_convention
        unit_a_plan[f"out{resolved_output_a}"] = True

        unit_b_plan = plan[resolved_unit_b]
        unit_b_plan["duty"] = command.duty
        unit_b_plan["phase_deg"] = command.phase_deg if resolved_unit_b != resolved_unit_a else 0
        unit_b_plan["dead_rise_ns"] = command.dead_rise_ns
        unit_b_plan["dead_fall_ns"] = command.dead_fall_ns
        unit_b_plan["switch_convention"] = command.switch_convention
        unit_b_plan[f"out{resolved_output_b}"] = True

        self._apply_pwm_plan(shell, plan, command, enable)
        return command

    def disable(
        self,
        pin: str,
        freq_hz: int = 1000,
        duty: float = 0.5,
        *,
        min_freq_hz: int | None = None,
        phase_deg: int = 0,
        dead_rise_ns: int = 0,
        dead_fall_ns: int = 0,
        modulation: int | None = None,
        switch_convention: int | str | None = None,
    ) -> PwmCommandValues:
        return self.set_pwm(
            pin=pin,
            freq_hz=freq_hz,
            duty=duty,
            enable=False,
            min_freq_hz=min_freq_hz,
            phase_deg=phase_deg,
            dead_rise_ns=dead_rise_ns,
            dead_fall_ns=dead_fall_ns,
            modulation=modulation,
            switch_convention=switch_convention,
        )

    def disable_pair(
        self,
        pin_a: str,
        pin_b: str,
        freq_hz: int = 1000,
        duty: float = 0.5,
        *,
        min_freq_hz: int | None = None,
        phase_deg: int = 0,
        dead_rise_ns: int = 0,
        dead_fall_ns: int = 0,
        modulation: int | None = None,
        switch_convention: int | str | None = None,
    ) -> PwmCommandValues:
        return self.set_pwm_pair(
            pin_a=pin_a,
            pin_b=pin_b,
            freq_hz=freq_hz,
            duty=duty,
            enable=False,
            min_freq_hz=min_freq_hz,
            phase_deg=phase_deg,
            dead_rise_ns=dead_rise_ns,
            dead_fall_ns=dead_fall_ns,
            modulation=modulation,
            switch_convention=switch_convention,
        )

    def init_burst_mode(self, pin: str) -> None:
        pwm_source = self._resolve_pin(pin)
        self._spin_pwm_action(int(pwm_source["tu"]), PWM_ACT_INIT_BURST)

    def configure_burst_mode(
        self,
        pin: str,
        *,
        off_cycles: int | None = None,
        total_cycles: int | None = None,
        bm_cmp: int | None = None,
        bm_per: int | None = None,
    ) -> None:
        burst_cmp = off_cycles if off_cycles is not None else bm_cmp
        burst_per = total_cycles if total_cycles is not None else bm_per

        if burst_cmp is None or burst_per is None:
            raise ValueError("configure_burst_mode requires off_cycles/total_cycles or bm_cmp/bm_per")
        if burst_cmp < 0:
            raise ValueError("burst_cmp must be >= 0")
        if burst_per <= 0:
            raise ValueError("burst_per must be > 0")
        if burst_cmp >= burst_per:
            raise ValueError("burst_cmp must be smaller than burst_per")

        pwm_source = self._resolve_pin(pin)
        self._spin_pwm_action(
            int(pwm_source["tu"]),
            PWM_ACT_SET_BURST,
            burst_cmp=burst_cmp,
            burst_per=burst_per,
        )

    def start_burst_mode(self, pin: str) -> None:
        pwm_source = self._resolve_pin(pin)
        self._spin_pwm_action(int(pwm_source["tu"]), PWM_ACT_START_BURST)

    def stop_burst_mode(self, pin: str) -> None:
        pwm_source = self._resolve_pin(pin)
        self._spin_pwm_action(int(pwm_source["tu"]), PWM_ACT_STOP_BURST)

    def deinit_burst_mode(self, pin: str) -> None:
        pwm_source = self._resolve_pin(pin)
        self._spin_pwm_action(int(pwm_source["tu"]), PWM_ACT_DEINIT_BURST)

    def read_state(self) -> CutPwmState:
        shell = self._require_shell()
        enabled_outputs: list[str] = []
        for pin_name, source in PWM_SOURCES.items():
            field_name = self._output_field(str(source["unit"]), int(source["output"]))
            value = self._to_bool(self._ts_get(shell, f"{CUT_PWM_BASE}/{field_name}"))
            if value:
                enabled_outputs.append(pin_name)
        return CutPwmState(
            enabled=self._to_bool(self._ts_get(shell, f"{CUT_PWM_BASE}/wEnable")),
            enabled_outputs=tuple(enabled_outputs),
            status=self._to_int(self._ts_get(shell, f"{CUT_PWM_BASE}/rStatus")),
        )

    def read_period(self, pin: str) -> int | None:
        shell = self._require_shell()
        target = self._resolve_pin(pin)
        unit_fields = PWM_FIELDS[str(target["unit"])]
        return self._to_int(self._ts_get(shell, f"{CUT_PWM_BASE}/{unit_fields['period']}"))

    def read_resolution_ps(self, pin: str) -> int | None:
        shell = self._require_shell()
        target = self._resolve_pin(pin)
        unit = str(target["unit"])
        cached_field = self._resolution_field_by_unit.get(unit)
        if cached_field is not None:
            return self._to_int(self._ts_get(shell, f"{CUT_PWM_BASE}/{cached_field}"))
        if unit in self._resolution_field_by_unit:
            return None
        for field_template in PWM_RESOLUTION_FIELD_CANDIDATES:
            field = field_template.format(unit=unit)
            path = f"{CUT_PWM_BASE}/{field}"
            try:
                value = self._to_int(self._ts_get(shell, path))
            except RuntimeError:
                continue
            if value is not None:
                self._resolution_field_by_unit[unit] = field
                return value
        self._resolution_field_by_unit[unit] = None
        return None

    def _resolve_pin(self, pin: str) -> dict[str, int | str]:
        normalized = pin.strip().upper()
        if normalized not in PWM_SOURCES:
            raise ValueError(f"Unknown PWM-capable CUT pin '{pin}'.")
        source = PWM_SOURCES[normalized]
        return {"unit": str(source["unit"]), "output": int(source["output"]), "tu": int(source["tu"])}

    def _resolve_unit_output(self, unit: str, output: int) -> tuple[str, int]:
        resolved_unit = unit.strip().upper()
        if resolved_unit not in PWM_FIELDS:
            raise ValueError(f"Unknown PWM unit '{unit}'. Expected one of: {', '.join(PWM_UNITS)}")
        if output not in (1, 2):
            raise ValueError("output must be 1 or 2")
        if output == 2 and PWM_FIELDS[resolved_unit]["out2"] is None:
            raise ValueError(f"PWM unit {resolved_unit} does not have output 2")
        return resolved_unit, output

    def _prepare_pwm_args(
        self,
        *,
        freq_hz: int,
        duty: float,
        min_freq_hz: int | None,
        phase_deg: int,
        dead_rise_ns: int,
        dead_fall_ns: int,
        modulation: int | None,
        switch_convention: int | str | None,
    ) -> PwmCommandValues:
        applied_freq_hz = resolve_pwm_frequency_hz(freq_hz)
        min_freq_value = resolve_pwm_min_frequency_hz(min_freq_hz, applied_freq_hz)
        applied_duty = clamp_pwm_duty(duty)
        applied_phase_deg = clamp_pwm_phase_deg(phase_deg)
        applied_modulation = DEFAULT_PWM_MODULATION if modulation is None else int(modulation)
        applied_switch_convention = resolve_switch_convention(switch_convention)
        self._report_clamp("freq_hz", freq_hz, applied_freq_hz)
        if min_freq_hz is not None:
            self._report_clamp("min_freq_hz", min_freq_hz, min_freq_value)
        self._report_clamp("duty", duty, applied_duty)
        self._report_clamp("phase_deg", phase_deg, applied_phase_deg)
        if dead_rise_ns < 0 or dead_fall_ns < 0:
            raise ValueError("dead time values must be >= 0")
        if applied_modulation not in (LFT_ALIGNED, UPDWN):
            raise ValueError(f"modulation must be {LFT_ALIGNED} (Lft_aligned) or {UPDWN} (UpDwn)")
        return PwmCommandValues(
            freq_hz=applied_freq_hz,
            min_freq_hz=min_freq_value,
            duty=applied_duty,
            phase_deg=applied_phase_deg,
            dead_rise_ns=dead_rise_ns,
            dead_fall_ns=dead_fall_ns,
            modulation=applied_modulation,
            switch_convention=applied_switch_convention,
        )

    def _empty_pwm_plan(self) -> dict[str, dict[str, int | float | bool]]:
        return {
            unit: {
                "out1": False,
                "out2": False,
                "duty": 0.0,
                "phase_deg": 0,
                "dead_rise_ns": 0,
                "dead_fall_ns": 0,
                "switch_convention": DEFAULT_SWITCH_CONVENTION,
            }
            for unit in PWM_UNITS
        }

    def _apply_pwm_plan(
        self,
        shell: ThingSetShell,
        plan: dict[str, dict[str, int | float | bool]],
        command: PwmCommandValues,
        enable: bool,
    ) -> None:
        self._ts_set(shell, f"{CUT_PWM_BASE}/wFreq_Hz", command.freq_hz)
        self._ts_set(shell, f"{CUT_PWM_BASE}/wMinFreq_Hz", command.min_freq_hz)
        self._ts_set(shell, f"{CUT_PWM_BASE}/wMod", command.modulation)

        for unit in PWM_UNITS:
            unit_fields = PWM_FIELDS[unit]
            unit_plan = plan[unit]
            self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['out1']}", bool(unit_plan["out1"]))
            if unit_fields["out2"] is not None:
                self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['out2']}", bool(unit_plan["out2"]))
            self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['duty']}", unit_plan["duty"])
            self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['phase']}", unit_plan["phase_deg"])
            self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['dead_rise']}", unit_plan["dead_rise_ns"])
            self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['dead_fall']}", unit_plan["dead_fall_ns"])
            self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['mod']}", command.modulation)
            self._ts_set(shell, f"{CUT_PWM_BASE}/{unit_fields['switch']}", unit_plan["switch_convention"])

        self._ts_set(shell, f"{CUT_PWM_BASE}/wEnable", enable)
        self._ts_set(shell, f"{CUT_PWM_BASE}/xApply", True)

    def _output_field(self, unit: str, output: int) -> str:
        unit_fields = PWM_FIELDS[unit]
        field = unit_fields["out1"] if output == 1 else unit_fields["out2"]
        if field is None:
            raise ValueError(f"PWM unit {unit} does not have output {output}")
        return str(field)

    def _report_clamp(self, name: str, requested: int | float, applied: int | float) -> None:
        if requested != applied:
            print(f"laptop clamp: {name} requested={requested} -> applied={applied}")

    def _ts_set(self, shell: ThingSetShell, path: str, value: object) -> None:
        payload = str(value).lower() if isinstance(value, bool) else str(value)
        ok, response = shell.set_value(path, payload)
        if not ok:
            raise RuntimeError(f"ThingSet write failed for {path}: {response}")

    def _ts_get(self, shell: ThingSetShell, path: str) -> str | None:
        ok, value = shell.get_value(path)
        if not ok:
            raise RuntimeError(f"ThingSet read failed for {path}")
        return value

    def _spin_pwm_action(
        self,
        tu: int,
        action: int,
        *,
        burst_cmp: int | None = None,
        burst_per: int | None = None,
    ) -> None:
        shell = self._require_shell()
        self._ts_set(shell, f"{SPIN_PWM_BASE}/wTU", tu)
        if burst_cmp is not None:
            self._ts_set(shell, f"{SPIN_PWM_BASE}/wBurstCmp", burst_cmp)
        if burst_per is not None:
            self._ts_set(shell, f"{SPIN_PWM_BASE}/wBurstPer", burst_per)
        self._ts_set(shell, f"{SPIN_PWM_BASE}/wAction", action)
        self._ts_set(shell, f"{SPIN_PWM_BASE}/xExec", True)

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
            raise RuntimeError("CUT PWM controller is not connected. Call connect() first.")
        return self._shell
