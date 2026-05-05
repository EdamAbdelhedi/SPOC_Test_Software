#!/usr/bin/env python3
"""Automatic phase-shift check between two selected PWM outputs."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR.parent / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from cut_pwm import (
    CutPwmController,
    PWM_SOURCES,
    clamp_pwm_duty,
    clamp_pwm_phase_deg,
    resolve_pwm_frequency_hz,
)  # type: ignore  # noqa: E402
from mux_controller import MuxController  # type: ignore  # noqa: E402
from oscilloscope import Oscilloscope  # type: ignore  # noqa: E402

SUPPORTED_PWM_PINS = tuple(sorted(PWM_SOURCES.keys()))
CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
MAIN_PWM_PINS = ("PA8", "PB12", "PB14", "PC8", "PC6")
MAIN_PWM_PHASE_PAIRS = (
    ("PA8", "PB12"),
    ("PA8", "PB14"),
    ("PA8", "PC8"),
    ("PA8", "PC6"),
    ("PB12", "PB14"),
    ("PB12", "PC8"),
    ("PB12", "PC6"),
    ("PB14", "PC8"),
    ("PB14", "PC6"),
    ("PC8", "PC6"),
)
PWM_PIN_LABELS = {
    "PA8": "PWM A",
    "PA9": "PWM A complementary",
    "PA10": "PWM B",
    "PB12": "PWM C",
    "PB13": "PWM C complementary",
    "PB14": "PWM D",
    "PB15": "PWM D complementary",
    "PC8": "PWM E",
    "PC9": "PWM E complementary",
    "PC6": "PWM F",
    "PC7": "PWM F complementary",
}
COMPLEMENTARY_PINS = {"PA9", "PB13", "PB15", "PC7", "PC9"}
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route two selected PWM outputs through the MUX and verify phase shift automatically"
    )
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--cut-pin-a", default="PA8", help="First PWM-capable CUT pin, for example PA8")
    parser.add_argument("--cut-pin-b", default="PB12", help="Second PWM-capable CUT pin, for example PB12")
    parser.add_argument(
        "--all-main-pairs",
        default="false",
        help="When true, run the phase-shift test on the built-in main PWM pair matrix",
    )
    parser.add_argument("--channel-a", default="ch1", help="Shield channel for the first PWM, for example ch1")
    parser.add_argument("--channel-b", default="ch2", help="Shield channel for the second PWM, for example ch2")
    parser.add_argument("--freq", type=int, default=1000, help="PWM frequency in Hz")
    parser.add_argument("--duty", type=float, default=0.50, help="Duty value as a fraction")
    parser.add_argument("--phase-deg", type=float, default=30.0, help="Expected target phase shift in degrees")
    parser.add_argument("--phase-tol-deg", type=float, default=5.0, help="Allowed phase error in degrees")
    parser.add_argument("--settle-seconds", type=float, default=0.1, help="Delay before reading the oscilloscope")
    parser.add_argument("--averages", type=int, default=3, help="Number of phase measurements to average")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--timebase-scale", type=float, help="Rigol timebase scale in seconds/div")
    return parser


def default_timebase_for_phase(freq_hz: int) -> float:
    return max(1.0 / max(freq_hz * 5.0, 1.0), 1e-6)


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


def normalize_phase_deg(phase_deg: float) -> float:
    wrapped = math.fmod(phase_deg, 360.0)
    if wrapped <= -180.0:
        wrapped += 360.0
    elif wrapped > 180.0:
        wrapped -= 360.0
    return wrapped


def phase_error_deg(measured_deg: float, expected_deg: float) -> float:
    return abs(normalize_phase_deg(measured_deg - expected_deg))


def is_complementary_pin(cut_pin: str) -> bool:
    return cut_pin in COMPLEMENTARY_PINS


def measurement_items_for_pins(cut_pin_a: str, cut_pin_b: str) -> tuple[str, str, str]:
    a_complementary = is_complementary_pin(cut_pin_a)
    b_complementary = is_complementary_pin(cut_pin_b)

    if a_complementary == b_complementary:
        return "RRPHase", "RRDelay", "rising-to-rising"
    if not a_complementary and b_complementary:
        return "FRPHase", "FRDelay", "falling-to-rising"
    return "RFPHase", "RFDelay", "rising-to-falling"


def phase_to_delay_seconds(phase_deg: float, freq_hz: int) -> float:
    if freq_hz <= 0:
        raise ValueError("freq must be > 0")
    return normalize_phase_deg(phase_deg) / 360.0 / float(freq_hz)


def print_route_diagnostics(
    cut: CutPwmController,
    mux: MuxController,
    *,
    channel_a: str,
    cut_pin_a: str,
    channel_b: str,
    cut_pin_b: str,
) -> None:
    cut_state = cut.read_state()
    route_a = mux.read_route(channel=channel_a, cut_pin=cut_pin_a)
    route_b = mux.read_route(channel=channel_b, cut_pin=cut_pin_b)
    source_a = PWM_SOURCES[cut_pin_a]
    source_b = PWM_SOURCES[cut_pin_b]
    print(
        f"route A {cut_pin_a} -> CUT(PWM{source_a['unit']}{source_a['output']}) via {route_a.mux}/{route_a.channel}"
    )
    print(
        f"route B {cut_pin_b} -> CUT(PWM{source_b['unit']}{source_b['output']}) via {route_b.mux}/{route_b.channel}"
    )
    print(f"readback CUT: enabled={cut_state.enabled} outputs={list(cut_state.enabled_outputs)} status={cut_state.status}")
    print(f"readback MUX A: channel={route_a.channel} mux={route_a.mux} input={route_a.input_index} enable={route_a.enabled}")
    print(f"readback MUX B: channel={route_b.channel} mux={route_b.mux} input={route_b.input_index} enable={route_b.enabled}")


def prepare_scope(
    scope: Oscilloscope,
    scope_channel_a: int,
    scope_channel_b: int,
    freq_hz: int,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> None:
    chosen_timebase_scale = (
        timebase_scale if timebase_scale is not None else default_timebase_for_phase(freq_hz)
    )
    for scope_channel in (scope_channel_a, scope_channel_b):
        scope.configure_calibrated_channel(
            scope_channel,
            channel_probe=SCOPE_CHANNEL_PROBE,
            channel_scale=SCOPE_CHANNEL_SCALE,
            offset_volts=0.0,
            coupling="DC",
            enabled=True,
        )
    scope.set_timebase(chosen_timebase_scale)
    scope.configure_edge_trigger(
        channel=scope_channel_a,
        trigger_edge_level=trigger_edge_level,
        slope="POS",
    )
    scope.run()


def try_average_phase_deg(
    scope: Oscilloscope,
    scope_channel_a: int,
    scope_channel_b: int,
    phase_item: str,
    averages: int,
    settle_seconds: float,
) -> float | None:
    values: list[float] = []
    for _ in range(max(1, averages)):
        time.sleep(max(0.0, settle_seconds))
        try:
            values.append(scope.read_delay(phase_item, scope_channel_a, scope_channel_b))
        except Exception:
            continue
    if not values:
        return None
    sin_sum = sum(math.sin(math.radians(value)) for value in values)
    cos_sum = sum(math.cos(math.radians(value)) for value in values)
    return math.degrees(math.atan2(sin_sum, cos_sum))


def measure_phase_step(
    scope: Oscilloscope,
    cut: CutPwmController,
    mux: MuxController,
    *,
    cut_pin_a: str,
    cut_pin_b: str,
    channel_a: str,
    channel_b: str,
    scope_channel_a: int,
    scope_channel_b: int,
    freq_hz: int,
    duty: float,
    phase_deg: float,
    phase_tol_deg: float,
    averages: int,
    settle_seconds: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> bool:
    phase_item, delay_item, edge_pair = measurement_items_for_pins(cut_pin_a, cut_pin_b)
    mux.route_pair(channel_a, cut_pin_a, channel_b, cut_pin_b)
    command = cut.set_pwm_pair(
        pin_a=cut_pin_a,
        pin_b=cut_pin_b,
        freq_hz=freq_hz,
        duty=duty,
        phase_deg=int(round(phase_deg)),
    )
    prepare_scope(
        scope,
        scope_channel_a,
        scope_channel_b,
        command.freq_hz,
        trigger_edge_level,
        timebase_scale,
    )
    time.sleep(max(settle_seconds, 0.0))
    measured_phase_deg = try_average_phase_deg(
        scope,
        scope_channel_a,
        scope_channel_b,
        phase_item,
        averages,
        max(settle_seconds / 2.0, 0.05),
    )
    if measured_phase_deg is None:
        print(f"phase measurement failed: the oscilloscope could not extract a valid {phase_item} value")
        return False

    expected_phase_deg = normalize_phase_deg(command.phase_deg)
    measured_phase_deg = normalize_phase_deg(measured_phase_deg)
    err_deg = phase_error_deg(measured_phase_deg, expected_phase_deg)
    measured_delay_us = phase_to_delay_seconds(measured_phase_deg, command.freq_hz) * 1e6
    expected_delay_us = phase_to_delay_seconds(expected_phase_deg, command.freq_hz) * 1e6
    passed = err_deg <= phase_tol_deg

    print("\nMeasurements")
    print(f"measurement mode: {phase_item} / {delay_item} ({edge_pair})")
    print(
        f"phase: expected = {expected_phase_deg:.2f} deg | measured = {measured_phase_deg:.2f} deg | "
        f"tolerance = {phase_tol_deg:.2f} deg | error = {err_deg:.2f} deg | result = {'PASS' if passed else 'FAIL'}"
    )
    print(
        f"delay: expected = {expected_delay_us:.2f} us | measured = {measured_delay_us:.2f} us"
    )
    print(f"overall: {'PASS' if passed else 'FAIL'}")
    print_route_diagnostics(
        cut,
        mux,
        channel_a=channel_a,
        cut_pin_a=cut_pin_a,
        channel_b=channel_b,
        cut_pin_b=cut_pin_b,
    )
    return passed


def run_phase_case(
    scope: Oscilloscope,
    cut: CutPwmController,
    mux: MuxController,
    *,
    cut_pin_a: str,
    cut_pin_b: str,
    channel_a: str,
    channel_b: str,
    scope_channel_a: int,
    scope_channel_b: int,
    freq_hz: int,
    duty: float,
    phase_deg: float,
    phase_tol_deg: float,
    averages: int,
    settle_seconds: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> bool:
    print("Configuration")
    print(f"- PWM A: {PWM_PIN_LABELS.get(cut_pin_a, cut_pin_a)} ({cut_pin_a}) -> {channel_a} -> Rigol CH{scope_channel_a}")
    print(f"- PWM B: {PWM_PIN_LABELS.get(cut_pin_b, cut_pin_b)} ({cut_pin_b}) -> {channel_b} -> Rigol CH{scope_channel_b}")
    print(f"- Requested frequency: {freq_hz} Hz")
    print(f"- Applied frequency: {resolve_pwm_frequency_hz(freq_hz)} Hz")
    print(f"- Requested duty: {duty * 100:.1f}%")
    print(f"- Applied duty: {clamp_pwm_duty(duty) * 100:.1f}%")
    print(f"- Requested phase shift on PWM B: {phase_deg:.2f} deg")
    print(f"- Applied phase shift on PWM B: {normalize_phase_deg(clamp_pwm_phase_deg(int(round(phase_deg)))):.2f} deg")
    print(f"- Phase tolerance: {phase_tol_deg:.2f} deg")

    print(
        f"\nTesting {PWM_PIN_LABELS.get(cut_pin_a, cut_pin_a)} against "
        f"{PWM_PIN_LABELS.get(cut_pin_b, cut_pin_b)}"
    )

    return measure_phase_step(
        scope,
        cut,
        mux,
        cut_pin_a=cut_pin_a,
        cut_pin_b=cut_pin_b,
        channel_a=channel_a,
        channel_b=channel_b,
        scope_channel_a=scope_channel_a,
        scope_channel_b=scope_channel_b,
        freq_hz=freq_hz,
        duty=duty,
        phase_deg=phase_deg,
        phase_tol_deg=phase_tol_deg,
        averages=averages,
        settle_seconds=settle_seconds,
        trigger_edge_level=trigger_edge_level,
        timebase_scale=timebase_scale,
    )


def main() -> int:
    args = build_parser().parse_args()

    try:
        run_all_main_pairs = parse_bool_flag(args.all_main_pairs)
        channel_a = normalize_channel(args.channel_a)
        channel_b = normalize_channel(args.channel_b)
        resolve_pwm_frequency_hz(args.freq)
        if channel_a == channel_b:
            raise ValueError("channel-a and channel-b must be different")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if run_all_main_pairs:
            pair_plan = list(MAIN_PWM_PHASE_PAIRS)
        else:
            cut_pin_a = normalize_cut_pin(args.cut_pin_a)
            cut_pin_b = normalize_cut_pin(args.cut_pin_b)
            if cut_pin_a == cut_pin_b:
                raise ValueError("cut-pin-a and cut-pin-b must be different")
            pair_plan = [(cut_pin_a, cut_pin_b)]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    scope_channel_a = CHANNEL_TO_SCOPE[channel_a]
    scope_channel_b = CHANNEL_TO_SCOPE[channel_b]
    scope = Oscilloscope()
    mux = MuxController(port=args.mux_port)
    cut = CutPwmController(port=args.cut_port)

    try:
        scope.connect()
        mux.connect()
        cut.connect()
        pass_count = 0
        fail_count = 0

        for index, (cut_pin_a, cut_pin_b) in enumerate(pair_plan, start=1):
            print(f"\n=== Phase-shift case {index}/{len(pair_plan)}: {cut_pin_a} / {cut_pin_b} ===")
            pair_passed = False
            try:
                pair_passed = run_phase_case(
                    scope,
                    cut,
                    mux,
                    cut_pin_a=cut_pin_a,
                    cut_pin_b=cut_pin_b,
                    channel_a=channel_a,
                    channel_b=channel_b,
                    scope_channel_a=scope_channel_a,
                    scope_channel_b=scope_channel_b,
                    freq_hz=args.freq,
                    duty=args.duty,
                    phase_deg=args.phase_deg,
                    phase_tol_deg=args.phase_tol_deg,
                    averages=args.averages,
                    settle_seconds=args.settle_seconds,
                    trigger_edge_level=args.trigger_edge_level,
                    timebase_scale=args.timebase_scale,
                )
            finally:
                try:
                    cut.disable_pair(
                        pin_a=cut_pin_a,
                        pin_b=cut_pin_b,
                        freq_hz=args.freq,
                        duty=args.duty,
                        phase_deg=int(round(args.phase_deg)),
                    )
                except Exception:
                    pass
                try:
                    mux.disable_route(channel_a, cut_pin_a)
                except Exception:
                    pass
                try:
                    mux.disable_route(channel_b, cut_pin_b)
                except Exception:
                    pass
                print("Cleanup: outputs returned to idle state.")

            if pair_passed:
                pass_count += 1
            else:
                fail_count += 1

        print(f"\nSummary: {pass_count} phase PASS, {fail_count} phase FAIL, total {len(pair_plan)}")
        return 0 if fail_count == 0 else 1
    finally:
        cut.close()
        mux.close()
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
