#!/usr/bin/env python3
"""Automatic rise/fall-time check for one PWM output."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR.parent / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from cut_pwm import CutPwmController, PWM_SOURCES, clamp_pwm_duty, resolve_pwm_frequency_hz  # type: ignore  # noqa: E402
from mux_controller import MuxController  # type: ignore  # noqa: E402
from oscilloscope import Oscilloscope  # type: ignore  # noqa: E402

SUPPORTED_PWM_PINS = tuple(sorted(PWM_SOURCES.keys()))
ALL_PWM_PINS = ("PA8", "PA9", "PA10", "PB12", "PB13", "PB14", "PB15", "PC6", "PC7", "PC8", "PC9")
CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route one CUT PWM signal through MUX to the BNC and measure rise/fall time"
    )
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--cut-pin", default="PA8", help="PWM-capable CUT pin name, for example PA8")
    parser.add_argument(
        "--all-pwm-pins",
        default="false",
        help="When true, run the rise/fall test on every built-in PWM-capable CUT pin",
    )
    parser.add_argument("--channel", default="ch1", help="Shield channel name, for example ch1")
    parser.add_argument("--freq", type=int, default=1000, help="PWM frequency in Hz")
    parser.add_argument("--duty", type=float, default=0.50, help="Duty value as a fraction")
    parser.add_argument("--settle-seconds", type=float, default=0.1, help="Delay before reading the oscilloscope")
    parser.add_argument("--averages", type=int, default=3, help="Number of rise/fall measurements to average")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--timebase-scale", type=float, help="Rigol timebase scale in seconds/div")
    parser.add_argument("--rise-max-ns", type=float, default=500.0, help="Maximum allowed rise time in ns")
    parser.add_argument("--fall-max-ns", type=float, default=500.0, help="Maximum allowed fall time in ns")
    return parser


def default_timebase_for_frequency(freq_hz: int) -> float:
    return max(1.0 / max(freq_hz * 5.0, 1.0), 1e-6)


def default_timebase_for_transition(freq_hz: int, transition_limit_ns: float) -> float:
    period_scale = default_timebase_for_frequency(freq_hz)
    edge_scale = max(transition_limit_ns * 20.0 * 1e-9, 1e-7)
    return min(period_scale, edge_scale)


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


def print_route_diagnostics(cut: CutPwmController, mux: MuxController, channel: str, cut_pin: str) -> None:
    cut_state = cut.read_state()
    mux_state = mux.read_route(channel=channel, cut_pin=cut_pin)
    pwm_source = PWM_SOURCES[cut_pin]
    print(
        f"route {cut_pin} -> CUT(PWM{pwm_source['unit']}{pwm_source['output']}) "
        f"via {mux_state.mux}/{mux_state.channel}"
    )
    print(f"readback CUT: enabled={cut_state.enabled} outputs={list(cut_state.enabled_outputs)} status={cut_state.status}")
    print(f"readback MUX: channel={mux_state.channel} mux={mux_state.mux} input={mux_state.input_index} enable={mux_state.enabled}")


def prepare_scope(
    scope: Oscilloscope,
    scope_channel: int,
    freq_hz: int,
    transition_limit_ns: float,
    trigger_slope: str,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> None:
    chosen_timebase_scale = (
        timebase_scale
        if timebase_scale is not None
        else default_timebase_for_transition(freq_hz, transition_limit_ns)
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
    scope.configure_edge_trigger(channel=scope_channel, trigger_edge_level=trigger_edge_level, slope=trigger_slope)
    scope.run()


def try_average_transition(scope: Oscilloscope, scope_channel: int, averages: int, settle_seconds: float, kind: str) -> float | None:
    values: list[float] = []
    for _ in range(max(1, averages)):
        time.sleep(max(0.0, settle_seconds))
        try:
            if kind == "rise":
                values.append(scope.read_rise_time(scope_channel))
            else:
                values.append(scope.read_fall_time(scope_channel))
        except Exception:
            continue
    if not values:
        return None
    return sum(values) / len(values)


def measure_transition(
    scope: Oscilloscope,
    *,
    kind: str,
    scope_channel: int,
    freq_hz: int,
    transition_limit_ns: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
    averages: int,
    settle_seconds: float,
) -> float | None:
    prepare_scope(
        scope,
        scope_channel,
        freq_hz,
        transition_limit_ns,
        "POS" if kind == "rise" else "NEG",
        trigger_edge_level,
        timebase_scale,
    )
    return try_average_transition(scope, scope_channel, averages, max(settle_seconds / 2.0, 0.1), kind)


def measure_rise_fall_step(
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
    rise_max_ns: float,
    fall_max_ns: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> bool:
    command = cut.set_pwm(pin=cut_pin, freq_hz=freq_hz, duty=duty)
    applied_freq_hz = command.freq_hz
    time.sleep(max(settle_seconds, 0.0))

    measured_rise_s = measure_transition(
        scope,
        kind="rise",
        scope_channel=scope_channel,
        freq_hz=applied_freq_hz,
        transition_limit_ns=rise_max_ns,
        trigger_edge_level=trigger_edge_level,
        timebase_scale=timebase_scale,
        averages=averages,
        settle_seconds=settle_seconds,
    )
    measured_fall_s = measure_transition(
        scope,
        kind="fall",
        scope_channel=scope_channel,
        freq_hz=applied_freq_hz,
        transition_limit_ns=fall_max_ns,
        trigger_edge_level=trigger_edge_level,
        timebase_scale=timebase_scale,
        averages=averages,
        settle_seconds=settle_seconds,
    )

    if measured_rise_s is None:
        print("rise-time measurement failed: the oscilloscope could not extract a valid rise time")
        return False
    if measured_fall_s is None:
        print("fall-time measurement failed: the oscilloscope could not extract a valid fall time")
        return False

    measured_rise_ns = measured_rise_s * 1e9
    measured_fall_ns = measured_fall_s * 1e9
    rise_pass = measured_rise_ns <= rise_max_ns
    fall_pass = measured_fall_ns <= fall_max_ns
    passed = rise_pass and fall_pass

    print("\nMeasurements")
    print(f"rise: expected <= {rise_max_ns:.2f} ns | measured = {measured_rise_ns:.2f} ns | result = {'PASS' if rise_pass else 'FAIL'}")
    print(f"fall: expected <= {fall_max_ns:.2f} ns | measured = {measured_fall_ns:.2f} ns | result = {'PASS' if fall_pass else 'FAIL'}")
    print(f"overall: {'PASS' if passed else 'FAIL'}")
    return passed


def run_rise_fall_case(
    scope: Oscilloscope,
    mux: MuxController,
    cut: CutPwmController,
    *,
    channel: str,
    cut_pin: str,
    scope_channel: int,
    freq_hz: int,
    duty: float,
    averages: int,
    settle_seconds: float,
    rise_max_ns: float,
    fall_max_ns: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> bool:
    prepare_scope(
        scope,
        scope_channel,
        resolve_pwm_frequency_hz(freq_hz),
        max(rise_max_ns, fall_max_ns),
        "POS",
        trigger_edge_level,
        timebase_scale,
    )
    print("Configuration")
    print(f"- PWM: {cut_pin} -> {channel} -> Rigol CH{scope_channel}")
    print(f"- Requested frequency: {freq_hz} Hz")
    print(f"- Applied frequency: {resolve_pwm_frequency_hz(freq_hz)} Hz")
    print(f"- Requested duty: {duty * 100:.1f}%")
    print(f"- Applied duty: {clamp_pwm_duty(duty) * 100:.1f}%")
    print(f"- Rise limit: {rise_max_ns} ns")
    print(f"- Fall limit: {fall_max_ns} ns")
    print(f"- Probe: x{SCOPE_CHANNEL_PROBE}")
    print(f"- Vertical scale: {SCOPE_CHANNEL_SCALE} V/div")
    mux.route(channel, cut_pin)
    print_route_diagnostics(cut, mux, channel, cut_pin)

    print("\nRunning measurement")
    return measure_rise_fall_step(
        scope,
        cut,
        channel=channel,
        cut_pin=cut_pin,
        scope_channel=scope_channel,
        freq_hz=freq_hz,
        duty=duty,
        averages=averages,
        settle_seconds=settle_seconds,
        rise_max_ns=rise_max_ns,
        fall_max_ns=fall_max_ns,
        trigger_edge_level=trigger_edge_level,
        timebase_scale=timebase_scale,
    )


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
        for index, cut_pin in enumerate(pin_plan, start=1):
            print(f"\n=== Rise/Fall case {index}/{len(pin_plan)}: {cut_pin} ===")
            case_passed = False
            try:
                case_passed = run_rise_fall_case(
                    scope,
                    mux,
                    cut,
                    channel=channel,
                    cut_pin=cut_pin,
                    scope_channel=scope_channel,
                    freq_hz=args.freq,
                    duty=args.duty,
                    averages=args.averages,
                    settle_seconds=args.settle_seconds,
                    rise_max_ns=args.rise_max_ns,
                    fall_max_ns=args.fall_max_ns,
                    trigger_edge_level=args.trigger_edge_level,
                    timebase_scale=args.timebase_scale,
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
                print("Cleanup: outputs returned to idle state.")

            if case_passed:
                pass_count += 1
            else:
                fail_count += 1

        total_steps = len(pin_plan)
        print(f"\nSummary: {pass_count} rise/fall PASS, {fail_count} rise/fall FAIL, total {total_steps}")
        return 0 if fail_count == 0 else 1
    finally:
        cut.close()
        mux.close()
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
