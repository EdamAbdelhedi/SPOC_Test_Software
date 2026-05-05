#!/usr/bin/env python3
"""End-to-end CUT + MUX + BNC duty-cycle check built on the reusable classes."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR.parent / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from cut_pwm import (  # type: ignore  # noqa: E402
    CutPwmController,
    PWM_SOURCES,
    clamp_pwm_duty,
    expected_pwm_pin_duty_percent,
    resolve_pwm_frequency_hz,
)
from mux_controller import MuxController  # type: ignore  # noqa: E402
from oscilloscope import Oscilloscope  # type: ignore  # noqa: E402

SUPPORTED_PWM_PINS = tuple(sorted(PWM_SOURCES.keys()))
ALL_PWM_PINS = ("PA8", "PA9", "PA10", "PB12", "PB13", "PB14", "PB15", "PC6", "PC7", "PC8", "PC9")
CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route one CUT PWM signal through MUX to the BNC and measure duty cycle"
    )
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--cut-pin", default="PA8", help="PWM-capable CUT pin name, for example PA8")
    parser.add_argument(
        "--all-pwm-pins",
        default="false",
        help="When true, run the duty-cycle test on every built-in PWM-capable CUT pin",
    )
    parser.add_argument("--channel", default="ch1", help="Shield channel name, for example ch1")
    parser.add_argument("--freq", type=int, default=1000, help="PWM frequency in Hz")
    parser.add_argument("--duty-values", nargs="+", type=float, default=[0.20, 0.70], help="Duty values as fractions")
    parser.add_argument("--settle-seconds", type=float, default=0.1, help="Delay before reading the oscilloscope")
    parser.add_argument("--averages", type=int, default=3, help="Number of duty measurements to average")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--timebase-scale", type=float, help="Rigol timebase scale in seconds/div")
    parser.add_argument("--duty-tol-pct", type=float, default=1.0, help="Allowed duty error in percent")
    return parser


def default_timebase_for_frequency(freq_hz: int) -> float:
    return max(1.0 / max(freq_hz * 2.5, 1.0), 1e-6)


def normalize_cut_pin(cut_pin: str) -> str:
    normalized = cut_pin.strip().upper()
    if normalized not in PWM_SOURCES:
        raise ValueError(
            f"Unknown PWM-capable CUT pin '{cut_pin}'. Supported pins: {', '.join(SUPPORTED_PWM_PINS)}"
        )
    return normalized


def normalize_channel(channel: str) -> str:
    normalized = channel.strip().lower()
    if normalized not in CHANNEL_TO_SCOPE:
        raise ValueError(f"Unknown channel '{channel}'. Expected one of: {', '.join(CHANNEL_TO_SCOPE)}")
    return normalized


def parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'. Use true/false.")


def normalize_duty_percent(raw: float) -> float:
    return raw * 100.0 if raw <= 1.0 else raw


def print_route_diagnostics(cut: CutPwmController, mux: MuxController, channel: str, cut_pin: str) -> None:
    cut_state = cut.read_state()
    mux_state = mux.read_route(channel=channel, cut_pin=cut_pin)
    pwm_source = PWM_SOURCES[cut_pin]
    print(
        f"route {cut_pin} -> CUT(PWM{pwm_source['unit']}{pwm_source['output']}) "
        f"via {mux_state.mux}/{mux_state.channel}"
    )
    print(
        "readback "
        f"CUT(enabled={cut_state.enabled}, outputs={list(cut_state.enabled_outputs)}, rStatus={cut_state.status}) "
        f"MUX(channel={mux_state.channel}, mux={mux_state.mux})"
    )


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


def pct_error(measured: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if abs(measured) < 1e-12 else float("inf")
    return abs(measured - expected) / abs(expected) * 100.0


def measure_duty_step(
    scope: Oscilloscope,
    cut: CutPwmController,
    *,
    channel: str,
    cut_pin: str,
    scope_channel: int,
    freq_hz: int,
    duty: float,
    averages: int,
    settle_seconds: float,
    duty_tol_pct: float,
) -> bool:
    command = cut.set_pwm(pin=cut_pin, freq_hz=freq_hz, duty=duty)
    time.sleep(max(settle_seconds, 0.0))
    measured_duty = try_average_duty(scope, scope_channel, averages, max(settle_seconds / 2.0, 0.1))
    expected_pin_duty = expected_pwm_pin_duty_percent(cut_pin, command.duty)

    if measured_duty is None:
        print("duty measurement failed: the oscilloscope could not extract a valid duty cycle")
        return False

    error_pct = pct_error(measured_duty, expected_pin_duty)
    passed = error_pct <= duty_tol_pct

    print(
        f"channel={channel} cut_pin={cut_pin} expected_duty={expected_pin_duty:.1f}% "
        f"measured_duty={measured_duty:.2f}% "
        f"error={error_pct:.2f}% tolerance={duty_tol_pct:.2f}% result={'PASS' if passed else 'FAIL'}"
    )
    return passed


def run_duty_case(
    scope: Oscilloscope,
    mux: MuxController,
    cut: CutPwmController,
    *,
    channel: str,
    cut_pin: str,
    scope_channel: int,
    freq_hz: int,
    duty_values: list[float],
    averages: int,
    settle_seconds: float,
    duty_tol_pct: float,
) -> tuple[int, int]:
    pass_count = 0
    fail_count = 0

    mux.route(channel, cut_pin)
    print_route_diagnostics(cut, mux, channel, cut_pin)

    total_steps = len(duty_values)
    for step, duty in enumerate(duty_values, start=1):
        applied_duty = clamp_pwm_duty(duty)
        applied_freq_hz = resolve_pwm_frequency_hz(freq_hz)
        expected_duty = expected_pwm_pin_duty_percent(cut_pin, applied_duty)
        print(
            f"\n[{step}/{total_steps}] Testing {cut_pin} on {channel} at requested freq={freq_hz} Hz "
            f"applied freq={applied_freq_hz} Hz "
            f"with requested command duty={duty * 100:.1f}% applied command duty={applied_duty * 100:.1f}% "
            f"expected pin duty={expected_duty:.1f}%"
        )
        if measure_duty_step(
            scope,
            cut,
            channel=channel,
            cut_pin=cut_pin,
            scope_channel=scope_channel,
            freq_hz=freq_hz,
            duty=duty,
            averages=averages,
            settle_seconds=settle_seconds,
            duty_tol_pct=duty_tol_pct,
        ):
            pass_count += 1
        else:
            fail_count += 1

    return pass_count, fail_count


def main() -> int:
    args = build_parser().parse_args()

    try:
        channel = normalize_channel(args.channel)
        run_all_pwm_pins = parse_bool_flag(args.all_pwm_pins)
        resolve_pwm_frequency_hz(args.freq)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if run_all_pwm_pins:
            pin_plan = list(ALL_PWM_PINS)
        else:
            pin_plan = [normalize_cut_pin(args.cut_pin)]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

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
        prepare_scope(
            scope,
            scope_channel,
            resolve_pwm_frequency_hz(args.freq),
            args.trigger_edge_level,
            args.timebase_scale,
        )
        for index, cut_pin in enumerate(pin_plan, start=1):
            print(f"\n=== Duty-cycle case {index}/{len(pin_plan)}: {cut_pin} ===")
            case_pass_count = 0
            case_fail_count = 0
            try:
                case_pass_count, case_fail_count = run_duty_case(
                    scope,
                    mux,
                    cut,
                    channel=channel,
                    cut_pin=cut_pin,
                    scope_channel=scope_channel,
                    freq_hz=args.freq,
                    duty_values=args.duty_values,
                    averages=args.averages,
                    settle_seconds=args.settle_seconds,
                    duty_tol_pct=args.duty_tol_pct,
                )
            finally:
                try:
                    cut.disable(pin=cut_pin, freq_hz=args.freq)
                except Exception:
                    pass
                try:
                    mux.disable_route(channel, cut_pin)
                except Exception:
                    pass
                print("Outputs returned to idle state.")

            pass_count += case_pass_count
            fail_count += case_fail_count

        total_steps = len(pin_plan) * len(args.duty_values)
        print(f"\nSummary: {pass_count} duty PASS, {fail_count} duty FAIL, total {total_steps}")
        return 0 if fail_count == 0 else 1
    finally:
        cut.close()
        mux.close()
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
