#!/usr/bin/env python3
"""Check left-aligned and center-aligned PWM modulation modes on CUT outputs."""

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
    LFT_ALIGNED,
    UPDWN,
    CutPwmController,
    PWM_SOURCES,
    expected_pwm_pin_duty_percent,
)
from mux_controller import MuxController  # type: ignore  # noqa: E402
from oscilloscope import Oscilloscope  # type: ignore  # noqa: E402


PWM_SEQUENCE = (
    ("left-aligned", LFT_ALIGNED),
    ("center-aligned", UPDWN),
)
SUPPORTED_PWM_PINS = tuple(sorted(PWM_SOURCES.keys()))
ALL_PWM_PINS = ("PA8", "PA9", "PA10", "PB12", "PB13", "PB14", "PB15", "PC6", "PC7", "PC8", "PC9")
CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check PWM left-aligned and center-aligned modulation modes")
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--cut-pin", default="PA8", help="PWM-capable CUT pin, default PA8")
    parser.add_argument(
        "--all-pwm-pins",
        default="false",
        help="When true, run the modulation test on every built-in PWM-capable CUT pin",
    )
    parser.add_argument("--channel", default="ch2", help="Shield channel name, for example ch2")
    parser.add_argument("--freq", type=int, default=1000, help="PWM frequency in Hz")
    parser.add_argument("--duty", type=float, default=0.50, help="Duty value as a fraction")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--timebase-scale", type=float, help="Rigol timebase scale in seconds/div")
    parser.add_argument("--measure-settle-seconds", type=float, default=0.2, help="Delay before reading the scope")
    parser.add_argument("--freq-tol-pct", type=float, default=5.0, help="Allowed frequency error in percent")
    parser.add_argument("--duty-tol-pct", type=float, default=1.0, help="Allowed duty error in percent")
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=5.0,
        help="How long to keep each modulation active before switching.",
    )
    return parser


def default_timebase_for_frequency(freq_hz: int) -> float:
    return max(1.0 / max(freq_hz * 2.5, 1.0), 1e-6)


def normalize_cut_pin(cut_pin: str) -> str:
    normalized = cut_pin.strip().upper()
    if normalized not in PWM_SOURCES:
        supported = ", ".join(SUPPORTED_PWM_PINS)
        raise ValueError(f"Unknown PWM-capable CUT pin '{cut_pin}'. Supported pins: {supported}")
    return normalized


def parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'. Use true/false.")


def normalize_channel(channel: str) -> str:
    normalized = channel.strip().lower()
    if normalized not in CHANNEL_TO_SCOPE:
        supported = ", ".join(CHANNEL_TO_SCOPE)
        raise ValueError(f"Unknown channel '{channel}'. Supported channels: {supported}")
    return normalized


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


def normalize_duty_percent(raw: float) -> float:
    return raw * 100.0 if raw <= 1.0 else raw


def pct_error(measured: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if abs(measured) < 1e-12 else float("inf")
    return abs(measured - expected) / abs(expected) * 100.0


def read_scope_measurements(scope: Oscilloscope, scope_channel: int) -> tuple[float | None, float | None]:
    measured_freq_hz: float | None = None
    measured_duty_pct: float | None = None

    try:
        measured_freq_hz = scope.read_frequency(scope_channel)
    except Exception:
        pass

    try:
        measured_duty_pct = normalize_duty_percent(scope.read_duty_cycle(scope_channel))
    except Exception:
        pass

    return measured_freq_hz, measured_duty_pct


def check_scope_measurements(
    scope: Oscilloscope,
    *,
    scope_channel: int,
    expected_freq_hz: int,
    expected_duty_pct: float,
    freq_tol_pct: float,
    duty_tol_pct: float,
) -> tuple[bool, float | None, float | None, float | None, float | None]:
    measured_freq_hz, measured_duty_pct = read_scope_measurements(scope, scope_channel)
    freq_pass = False
    duty_pass = False
    freq_error_pct: float | None = None
    duty_error_pct: float | None = None

    if measured_freq_hz is not None:
        freq_error_pct = pct_error(measured_freq_hz, expected_freq_hz)
        freq_pass = freq_error_pct <= freq_tol_pct

    if measured_duty_pct is not None:
        duty_error_pct = pct_error(measured_duty_pct, expected_duty_pct)
        duty_pass = duty_error_pct <= duty_tol_pct

    return freq_pass and duty_pass, measured_freq_hz, measured_duty_pct, freq_error_pct, duty_error_pct


def read_state(cut: CutPwmController, mux: MuxController, channel: str, cut_pin: str) -> tuple[int | None, bool, str, int, bool]:
    state = cut.read_state()
    route = mux.read_route(channel=channel, cut_pin=cut_pin)
    period = cut.read_period(cut_pin)
    output_enabled = state.enabled and cut_pin in state.enabled_outputs and state.status == 0
    return period, output_enabled, route.mux, route.input_index, route.enabled


def format_freq(value: float | None) -> str:
    return "invalid" if value is None else f"{value:.3g}Hz"


def format_duty(value: float | None) -> str:
    return "invalid" if value is None else f"{value:.2f}%"


def format_error(value: float | None) -> str:
    return "invalid" if value is None else f"{value:.2f}%"


def print_period_ratio_check(periods: dict[str, int | None]) -> bool:
    left_period = periods.get("left-aligned")
    center_period = periods.get("center-aligned")

    if left_period is None or center_period is None:
        print(f"period ratio   result=FAIL | left={left_period} center={center_period}")
        return False

    expected_left_period = center_period * 2
    passed = left_period == expected_left_period
    print(f"period ratio   result={'PASS' if passed else 'FAIL'} | left={left_period} center={center_period}")
    return passed


def run_modulation_case(
    scope: Oscilloscope,
    mux: MuxController,
    cut: CutPwmController,
    *,
    channel: str,
    cut_pin: str,
    scope_channel: int,
    freq_hz: int,
    duty: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
    measure_settle_seconds: float,
    step_seconds: float,
    freq_tol_pct: float,
    duty_tol_pct: float,
) -> tuple[int, int]:
    pwm_source = PWM_SOURCES[cut_pin]
    pass_count = 0
    fail_count = 0
    expected_duty_pct = expected_pwm_pin_duty_percent(cut_pin, duty)
    periods: dict[str, int | None] = {}

    print(
        f"{cut_pin} PWM{pwm_source['unit']}{pwm_source['output']} -> {channel}/CH{scope_channel} | "
        f"{freq_hz}Hz, duty={duty * 100:.1f}%, trigger={trigger_edge_level}V"
    )

    mux.route(channel, cut_pin)
    prepare_scope(scope, scope_channel, freq_hz, trigger_edge_level, timebase_scale)

    for mode_name, modulation in PWM_SEQUENCE:
        cut.set_pwm(
            pin=cut_pin,
            freq_hz=freq_hz,
            duty=duty,
            modulation=modulation,
        )
        time.sleep(max(0.0, measure_settle_seconds))
        period, output_enabled, mux_name, mux_input, route_enabled = read_state(cut, mux, channel, cut_pin)
        periods[mode_name] = period
        scope_passed, measured_freq_hz, measured_duty_pct, freq_error_pct, duty_error_pct = check_scope_measurements(
            scope,
            scope_channel=scope_channel,
            expected_freq_hz=freq_hz,
            expected_duty_pct=expected_duty_pct,
            freq_tol_pct=freq_tol_pct,
            duty_tol_pct=duty_tol_pct,
        )
        output_text = "OK" if output_enabled else "BAD"
        route_text = "OK" if route_enabled else "BAD"
        result_text = "PASS" if scope_passed else "FAIL"
        print(
            f"{mode_name:<14} result={result_text} | period={period} | "
            f"freq={format_freq(measured_freq_hz)} err={format_error(freq_error_pct)} | "
            f"duty={format_duty(measured_duty_pct)} err={format_error(duty_error_pct)} | "
            f"CUT={output_text} route={mux_name}/{mux_input}:{route_text}"
        )
        if scope_passed:
            pass_count += 1
        else:
            fail_count += 1
        remaining_step_seconds = max(0.0, step_seconds - max(0.0, measure_settle_seconds))
        time.sleep(remaining_step_seconds)

    if print_period_ratio_check(periods):
        pass_count += 1
    else:
        fail_count += 1

    return pass_count, fail_count


def main() -> int:
    args = build_parser().parse_args()

    try:
        channel = normalize_channel(args.channel)
        run_all_pwm_pins = parse_bool_flag(args.all_pwm_pins)
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
    mux = MuxController(port=args.mux_port)
    cut = CutPwmController(port=args.cut_port)
    scope = Oscilloscope()
    pass_count = 0
    fail_count = 0

    try:
        mux.connect()
        cut.connect()
        scope.connect()

        for index, cut_pin in enumerate(pin_plan, start=1):
            print(f"\n[{index}/{len(pin_plan)}] {cut_pin}")
            case_pass_count = 0
            case_fail_count = 0
            try:
                case_pass_count, case_fail_count = run_modulation_case(
                    scope,
                    mux,
                    cut,
                    channel=channel,
                    cut_pin=cut_pin,
                    scope_channel=scope_channel,
                    freq_hz=args.freq,
                    duty=args.duty,
                    trigger_edge_level=args.trigger_edge_level,
                    timebase_scale=args.timebase_scale,
                    measure_settle_seconds=args.measure_settle_seconds,
                    step_seconds=args.step_seconds,
                    freq_tol_pct=args.freq_tol_pct,
                    duty_tol_pct=args.duty_tol_pct,
                )
            finally:
                try:
                    cut.disable(pin=cut_pin, freq_hz=args.freq, duty=args.duty)
                except Exception:
                    pass
                try:
                    mux.disable_route(channel, cut_pin)
                except Exception:
                    pass
                print("idle")

            pass_count += case_pass_count
            fail_count += case_fail_count

        total_checks = len(pin_plan) * (len(PWM_SEQUENCE) + 1)
        print(f"\nSummary: {pass_count} modulation checks PASS, {fail_count} modulation checks FAIL, total {total_checks}")
        return 0 if fail_count == 0 else 1
    except KeyboardInterrupt:
        print("\nStopping PWM.")
        return 0
    finally:
        scope.close()
        cut.close()
        mux.close()


if __name__ == "__main__":
    raise SystemExit(main())
