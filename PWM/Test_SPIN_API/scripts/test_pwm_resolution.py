#!/usr/bin/env python3
"""Measure one PWM signal and check timer resolution for one prescaler range."""

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

from cut_pwm import CutPwmController, PWM_SOURCES, clamp_pwm_duty, resolve_pwm_frequency_hz  # type: ignore  # noqa: E402
from mux_controller import MuxController  # type: ignore  # noqa: E402
from oscilloscope import Oscilloscope  # type: ignore  # noqa: E402


SUPPORTED_PWM_PINS = tuple(sorted(PWM_SOURCES.keys()))
ALL_PWM_PINS = ("PA8", "PA10", "PB12", "PB14", "PC8", "PC6")
CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0

# For HRTIM frequency 170 MHz, from the OwnTech PwmHAL documentation.
PRESCALER_TABLE = (
    (83000, 0, 184),
    (41500, 1, 368),
    (20800, 2, 735),
    (10400, 3, 1470),
    (5200, 4, 2940),
    (2600, 5, 5880),
    (1300, 6, 11760),
    (650, 7, 23530),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route one CUT PWM signal and verify selected HRTIM resolution"
    )
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--cut-pin", default="PA8", help="PWM-capable CUT pin name, for example PA8")
    parser.add_argument(
        "--all-pwm-pins",
        default="false",
        help="When true, run one output per PWM timing unit",
    )
    parser.add_argument("--channel", default="ch1", help="Shield channel name, for example ch1")
    parser.add_argument(
        "--freq-values",
        nargs="+",
        type=int,
        default=[6000],
        help="PWM frequency in Hz. Only the first value is used because min_freq selects the prescaler at init.",
    )
    parser.add_argument(
        "--min-freq",
        type=int,
        help="Minimal frequency in Hz used for initVariableFrequency. Defaults to the first frequency.",
    )
    parser.add_argument("--duty", type=float, default=0.50, help="Duty value as a fraction")
    parser.add_argument("--settle-seconds", type=float, default=0.1, help="Delay before reading the oscilloscope")
    parser.add_argument("--averages", type=int, default=3, help="Number of frequency measurements to average")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--timebase-scale", type=float, help="Rigol timebase scale in seconds/div")
    parser.add_argument("--freq-tol-pct", type=float, default=5.0, help="Allowed frequency error in percent")
    parser.add_argument("--resolution-tol-ps", type=int, default=10, help="Allowed resolution error in picoseconds")
    return parser


def parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'. Use true/false.")


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


def expected_resolution_for_min_frequency(min_freq_hz: int) -> tuple[int, int]:
    for row_min_freq_hz, prscl, resolution_ps in PRESCALER_TABLE:
        if min_freq_hz >= row_min_freq_hz:
            return prscl, resolution_ps
    return PRESCALER_TABLE[-1][1], PRESCALER_TABLE[-1][2]


def closest_prescaler_for_resolution(resolution_ps: int | None) -> tuple[int, int] | None:
    if resolution_ps is None:
        return None
    _, prscl, table_resolution_ps = min(
        PRESCALER_TABLE,
        key=lambda row: abs(row[2] - resolution_ps),
    )
    return prscl, table_resolution_ps


def infer_resolution_ps(freq_hz: int, period_ticks: int | None) -> int | None:
    if period_ticks is None or period_ticks <= 0:
        return None
    # The CUT default modulation is center-aligned, where one PWM cycle spans
    # up-count and down-count halves.
    return int(round(1_000_000_000_000.0 / (float(freq_hz) * float(period_ticks) * 2.0)))


def default_timebase_for_frequency(freq_hz: int) -> float:
    return max(1.0 / max(freq_hz * 1.0, 1.0), 1e-6)


def prepare_scope(
    scope: Oscilloscope,
    scope_channel: int,
    freq_hz: int,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> None:
    chosen_timebase_scale = timebase_scale if timebase_scale is not None else default_timebase_for_frequency(freq_hz)
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


def try_average_frequency(scope: Oscilloscope, scope_channel: int, averages: int, settle_seconds: float) -> float | None:
    values: list[float] = []
    for _ in range(max(1, averages)):
        time.sleep(max(0.0, settle_seconds))
        try:
            values.append(scope.read_frequency(scope_channel))
        except Exception:
            continue
    if not values:
        return None
    return sum(values) / len(values)


def pct_error(measured: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if abs(measured) < 1e-12 else float("inf")
    return abs(measured - expected) / abs(expected) * 100.0


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
        f"MUX(channel={mux_state.channel}, mux={mux_state.mux}, rChannel={mux_state.input_index}, rEnable={mux_state.enabled})"
    )


def try_deinit_cut(cut: CutPwmController, cut_pin: str, *, context: str) -> None:
    try:
        cut.deinit(cut_pin)
        print(f"CUT deinit complete ({context}).")
    except Exception as exc:
        print(f"CUT deinit skipped ({context}): {exc}")


def measure_resolution_step(
    scope: Oscilloscope,
    cut: CutPwmController,
    *,
    channel: str,
    cut_pin: str,
    scope_channel: int,
    freq_hz: int,
    min_freq_hz: int,
    duty: float,
    averages: int,
    settle_seconds: float,
    freq_tol_pct: float,
    resolution_tol_ps: int,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> bool:
    expected_prscl, expected_resolution_ps = expected_resolution_for_min_frequency(min_freq_hz)
    command = cut.set_pwm(pin=cut_pin, freq_hz=freq_hz, min_freq_hz=min_freq_hz, duty=duty)
    prepare_scope(scope, scope_channel, command.freq_hz, trigger_edge_level, timebase_scale)
    time.sleep(max(settle_seconds, 0.0))

    measured_freq_hz = try_average_frequency(scope, scope_channel, averages, max(settle_seconds / 2.0, 0.1))
    period_ticks = cut.read_period(cut_pin)
    measured_resolution_ps = cut.read_resolution_ps(cut_pin)
    resolution_source = "direct"
    if measured_resolution_ps is None:
        measured_resolution_ps = infer_resolution_ps(command.freq_hz, period_ticks)
        resolution_source = "period"

    freq_ok = measured_freq_hz is not None and pct_error(measured_freq_hz, command.freq_hz) <= freq_tol_pct
    res_ok = (
        measured_resolution_ps is not None
        and abs(measured_resolution_ps - expected_resolution_ps) <= resolution_tol_ps
    )
    passed = freq_ok and res_ok

    freq_text = "invalid" if measured_freq_hz is None else f"{measured_freq_hz:.2f}Hz"
    freq_error_text = (
        "invalid" if measured_freq_hz is None else f"{pct_error(measured_freq_hz, command.freq_hz):.2f}%"
    )
    resolution_text = "invalid" if measured_resolution_ps is None else f"{measured_resolution_ps}ps"
    resolution_error_text = (
        "invalid" if measured_resolution_ps is None else f"{abs(measured_resolution_ps - expected_resolution_ps)}ps"
    )
    freq_result = "PASS" if freq_ok else "FAIL"
    resolution_result = "PASS" if res_ok else "FAIL"
    overall_result = "PASS" if passed else "FAIL"
    actual_prescaler = closest_prescaler_for_resolution(measured_resolution_ps)

    print("Frequency check")
    print(f"  expected : {command.freq_hz} Hz")
    print(f"  measured : {freq_text}")
    print(f"  error    : {freq_error_text} (tolerance {freq_tol_pct:.2f}%)")
    print(f"  result   : {freq_result}")
    print("Resolution check")
    print(f"  min freq : {min_freq_hz} Hz")
    print(f"  prescaler: PRSCL {expected_prscl}")
    print(f"  expected : {expected_resolution_ps} ps")
    print(f"  measured : {resolution_text} ({resolution_source}, period={period_ticks})")
    print(f"  error    : {resolution_error_text} (tolerance {resolution_tol_ps} ps)")
    if actual_prescaler is not None and actual_prescaler[0] != expected_prscl:
        print(
            f"  note     : measured resolution is closest to PRSCL {actual_prescaler[0]} "
            f"({actual_prescaler[1]} ps), not PRSCL {expected_prscl}"
        )
        print("             reset the CUT before changing the resolution/prescaler test case")
    print(f"  result   : {resolution_result}")
    print(f"Overall: {overall_result} | route={channel}/CH{scope_channel}")
    return passed


def run_resolution_case(
    scope: Oscilloscope,
    mux: MuxController,
    cut: CutPwmController,
    *,
    channel: str,
    cut_pin: str,
    scope_channel: int,
    freq_hz: int,
    min_freq_hz: int,
    duty: float,
    averages: int,
    settle_seconds: float,
    freq_tol_pct: float,
    resolution_tol_ps: int,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> tuple[int, int]:
    pass_count = 0
    fail_count = 0

    prepare_scope(scope, scope_channel, freq_hz, trigger_edge_level, timebase_scale)
    print(
        f"Initial state: preparing one PWM route on {cut_pin} and {channel}. "
        f"min_freq={min_freq_hz}Hz, duty={clamp_pwm_duty(duty) * 100.0:.1f}%"
    )
    try_deinit_cut(cut, cut_pin, context="before resolution case")
    mux.route(channel, cut_pin)
    print_route_diagnostics(cut, mux, channel, cut_pin)

    print(f"\n[1/1] Testing {cut_pin} at {freq_hz} Hz")
    if measure_resolution_step(
        scope,
        cut,
        channel=channel,
        cut_pin=cut_pin,
        scope_channel=scope_channel,
        freq_hz=freq_hz,
        min_freq_hz=min_freq_hz,
        duty=duty,
        averages=averages,
        settle_seconds=settle_seconds,
        freq_tol_pct=freq_tol_pct,
        resolution_tol_ps=resolution_tol_ps,
        trigger_edge_level=trigger_edge_level,
        timebase_scale=timebase_scale,
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
        freq_hz = resolve_pwm_frequency_hz(args.freq_values[0])
        min_freq_hz = resolve_pwm_frequency_hz(args.min_freq) if args.min_freq else freq_hz
        if freq_hz < min_freq_hz:
            raise ValueError("freq must be >= min-freq")
        if not math.isfinite(args.duty):
            raise ValueError("duty must be finite")
        if args.averages <= 0:
            raise ValueError("averages must be > 0")
        if args.freq_tol_pct < 0:
            raise ValueError("freq-tol-pct must be >= 0")
        if args.resolution_tol_ps < 0:
            raise ValueError("resolution-tol-ps must be >= 0")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        pin_plan = list(ALL_PWM_PINS) if run_all_pwm_pins else [normalize_cut_pin(args.cut_pin)]
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
            print(f"\n=== Resolution case {index}/{len(pin_plan)}: {cut_pin} ===")
            try:
                case_pass_count, case_fail_count = run_resolution_case(
                    scope,
                    mux,
                    cut,
                    channel=channel,
                    cut_pin=cut_pin,
                    scope_channel=scope_channel,
                    freq_hz=freq_hz,
                    min_freq_hz=min_freq_hz,
                    duty=args.duty,
                    averages=args.averages,
                    settle_seconds=args.settle_seconds,
                    freq_tol_pct=args.freq_tol_pct,
                    resolution_tol_ps=args.resolution_tol_ps,
                    trigger_edge_level=args.trigger_edge_level,
                    timebase_scale=args.timebase_scale,
                )
            finally:
                try:
                    cut.disable(pin=cut_pin, freq_hz=freq_hz, min_freq_hz=min_freq_hz, duty=args.duty)
                except Exception:
                    pass
                try_deinit_cut(cut, cut_pin, context="after resolution case")
                try:
                    mux.disable_route(channel, cut_pin)
                except Exception:
                    pass
                print("Output returned to idle state.")

            pass_count += case_pass_count
            fail_count += case_fail_count

        total_steps = len(pin_plan)
        print(f"\nSummary: {pass_count} resolution PASS, {fail_count} resolution FAIL, total {total_steps}")
        return 0 if fail_count == 0 else 1
    finally:
        cut.close()
        mux.close()
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
