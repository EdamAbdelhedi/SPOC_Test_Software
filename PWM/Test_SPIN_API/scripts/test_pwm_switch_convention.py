#!/usr/bin/env python3
"""Check which PWM output follows the commanded duty cycle."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR.parent / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from cut_pwm import CutPwmController, PWM_SOURCES, clamp_pwm_duty, resolve_switch_convention  # type: ignore  # noqa: E402
from mux_controller import MuxController  # type: ignore  # noqa: E402
from oscilloscope import Oscilloscope  # type: ignore  # noqa: E402


CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
PWM_UNIT_PINS = {
    "A": ("PA8", "PA9"),
    "C": ("PB12", "PB13"),
    "D": ("PB14", "PB15"),
    "E": ("PC8", "PC9"),
    "F": ("PC6", "PC7"),
}
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check PWM switch convention by measuring both unit outputs")
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--unit", default="A", help="PWM unit with two outputs: A, C, D, E, or F")
    parser.add_argument(
        "--all-pwm-units",
        default="false",
        help="When true, run the switch-convention test on every two-output PWM unit",
    )
    parser.add_argument("--channel", default="ch1", help="Shield channel name, for example ch1")
    parser.add_argument("--freq", type=int, default=1000, help="PWM frequency in Hz")
    parser.add_argument("--duty", type=float, default=0.30, help="Duty value as a fraction")
    parser.add_argument(
        "--convention",
        choices=("PWMx1", "PWMx2", "both"),
        default="PWMx1",
        help="Switch convention to command and verify.",
    )
    parser.add_argument("--settle-seconds", type=float, default=0.2, help="Delay before reading the oscilloscope")
    parser.add_argument("--averages", type=int, default=3, help="Number of duty measurements to average")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--timebase-scale", type=float, help="Rigol timebase scale in seconds/div")
    parser.add_argument("--duty-tol-pct", type=float, default=1.0, help="Allowed absolute duty error in percent")
    return parser


def normalize_unit(unit: str) -> str:
    normalized = unit.strip().upper()
    if normalized not in PWM_UNIT_PINS:
        raise ValueError(f"Unknown two-output PWM unit '{unit}'. Supported units: {', '.join(PWM_UNIT_PINS)}")
    return normalized


def normalize_channel(channel: str) -> str:
    normalized = channel.strip().lower()
    if normalized not in CHANNEL_TO_SCOPE:
        raise ValueError(f"Unknown channel '{channel}'. Supported channels: {', '.join(CHANNEL_TO_SCOPE)}")
    return normalized


def parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'. Use true/false.")


def default_timebase_for_frequency(freq_hz: int) -> float:
    return max(1.0 / max(freq_hz * 2.5, 1.0), 1e-6)


def normalize_duty_percent(raw: float) -> float:
    return raw * 100.0 if raw <= 1.0 else raw


def prepare_scope(
    scope: Oscilloscope,
    scope_channel: int,
    freq_hz: int,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> None:
    chosen_timebase_scale = (
        timebase_scale if timebase_scale is not None else default_timebase_for_frequency(freq_hz)
    )
    scope.configure_calibrated_channel(
        scope_channel,
        channel_probe=SCOPE_CHANNEL_PROBE,
        channel_scale=SCOPE_CHANNEL_SCALE,
        offset_volts=0.0,
        coupling="DC",
        enabled=True,
    )
    scope.set_timebase(chosen_timebase_scale)
    scope.configure_edge_trigger(channel=scope_channel, trigger_edge_level=trigger_edge_level, slope="POS")
    scope.run()


def try_average_duty(scope: Oscilloscope, scope_channel: int, averages: int, settle_seconds: float) -> float | None:
    values: list[float] = []
    for _ in range(max(1, averages)):
        time.sleep(max(0.0, settle_seconds))
        try:
            values.append(normalize_duty_percent(scope.read_duty_cycle(scope_channel)))
        except Exception:
            continue
    if not values:
        return None
    return sum(values) / len(values)


def expected_pin_duty_percent(pin: str, command_duty: float, convention: str) -> float:
    output = int(PWM_SOURCES[pin]["output"])
    controlled_output = 1 if convention == "PWMx1" else 2
    primary_duty = clamp_pwm_duty(command_duty) * 100.0
    return primary_duty if output == controlled_output else 100.0 - primary_duty


def measure_pin(
    scope: Oscilloscope,
    mux: MuxController,
    *,
    channel: str,
    pin: str,
    scope_channel: int,
    expected_duty_pct: float,
    averages: int,
    settle_seconds: float,
    duty_tol_pct: float,
) -> bool:
    mux.route(channel, pin)
    measured_duty = try_average_duty(scope, scope_channel, averages, max(settle_seconds / 2.0, 0.1))
    if measured_duty is None:
        print(f"{pin} result=FAIL | expected={expected_duty_pct:.2f}% measured=invalid")
        return False

    error_pct = abs(measured_duty - expected_duty_pct)
    passed = error_pct <= duty_tol_pct
    print(
        f"{pin} result={'PASS' if passed else 'FAIL'} | expected={expected_duty_pct:.2f}% "
        f"measured={measured_duty:.2f}% error={error_pct:.2f}% tolerance={duty_tol_pct:.2f}%"
    )
    return passed


def run_switch_convention_case(
    scope: Oscilloscope,
    mux: MuxController,
    cut: CutPwmController,
    *,
    unit: str,
    convention: str,
    channel: str,
    scope_channel: int,
    freq_hz: int,
    duty: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
    averages: int,
    settle_seconds: float,
    duty_tol_pct: float,
) -> tuple[int, int]:
    output1_pin, output2_pin = PWM_UNIT_PINS[unit]
    output_pins = (output1_pin, output2_pin)
    pass_count = 0
    fail_count = 0

    prepare_scope(scope, scope_channel, freq_hz, trigger_edge_level, timebase_scale)
    print(
        f"PWM{unit} switch convention check on {output1_pin}/{output2_pin}: "
        f"expected {convention}, freq={freq_hz}Hz, duty={clamp_pwm_duty(duty) * 100.0:.1f}%"
    )

    switch_convention = resolve_switch_convention(convention)
    cut.set_pwm_pair(
        output1_pin,
        output2_pin,
        freq_hz=freq_hz,
        duty=duty,
        switch_convention=switch_convention,
    )
    time.sleep(max(0.0, settle_seconds))

    try:
        for pin in output_pins:
            expected_duty = expected_pin_duty_percent(pin, duty, convention)
            if measure_pin(
                scope,
                mux,
                channel=channel,
                pin=pin,
                scope_channel=scope_channel,
                expected_duty_pct=expected_duty,
                averages=averages,
                settle_seconds=settle_seconds,
                duty_tol_pct=duty_tol_pct,
            ):
                pass_count += 1
            else:
                fail_count += 1
    finally:
        try:
            cut.disable_pair(
                output1_pin,
                output2_pin,
                freq_hz=freq_hz,
                duty=duty,
                switch_convention=switch_convention,
            )
        except Exception:
            pass
        for pin in output_pins:
            try:
                mux.disable_route(channel, pin)
            except Exception:
                pass

    return pass_count, fail_count


def main() -> int:
    args = build_parser().parse_args()

    try:
        run_all_pwm_units = parse_bool_flag(args.all_pwm_units)
        channel = normalize_channel(args.channel)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        units = list(PWM_UNIT_PINS) if run_all_pwm_units else [normalize_unit(args.unit)]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    conventions = ("PWMx1", "PWMx2") if args.convention == "both" else (args.convention,)

    scope_channel = CHANNEL_TO_SCOPE[channel]
    scope = Oscilloscope()
    mux = MuxController(port=args.mux_port)
    cut = CutPwmController(port=args.cut_port)
    pass_count = 0
    fail_count = 0

    try:
        scope.connect()
        mux.connect()
        cut.connect()

        for unit_index, unit in enumerate(units, start=1):
            for convention_index, convention in enumerate(conventions, start=1):
                print(
                    f"\n[{unit_index}/{len(units)} unit, {convention_index}/{len(conventions)} convention] "
                    f"PWM{unit} {convention}"
                )
                case_pass_count, case_fail_count = run_switch_convention_case(
                    scope,
                    mux,
                    cut,
                    unit=unit,
                    convention=convention,
                    channel=channel,
                    scope_channel=scope_channel,
                    freq_hz=args.freq,
                    duty=args.duty,
                    trigger_edge_level=args.trigger_edge_level,
                    timebase_scale=args.timebase_scale,
                    averages=args.averages,
                    settle_seconds=args.settle_seconds,
                    duty_tol_pct=args.duty_tol_pct,
                )
                pass_count += case_pass_count
                fail_count += case_fail_count

        total_checks = len(units) * len(conventions) * 2
        print(f"\nSummary: {pass_count} switch-convention PASS, {fail_count} FAIL, total {total_checks}")
        return 0 if fail_count == 0 else 1
    finally:
        cut.close()
        mux.close()
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
