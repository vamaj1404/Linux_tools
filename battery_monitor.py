#!/usr/bin/env python3
import curses
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

SAMPLE_INTERVAL = 5.0
MAX_SAMPLES = 720          # 1 hour with a 5-second interval
PREFERRED_CHART_HEIGHT = 12

ENV = os.environ.copy()
ENV["LC_ALL"] = "C"


@dataclass
class Sample:
    timestamp: str
    power_w: Optional[float]
    cpu_pct: Optional[float]
    ram_pct: float
    battery_pct: Optional[float]
    time_left_min: Optional[float]
    time_left_text: str
    state: str


def run_command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
        env=ENV,
    )
    return result.stdout


def find_battery_device() -> str:
    devices = run_command(["upower", "-e"]).splitlines()
    batteries = [d.strip() for d in devices if "battery" in d.lower() or "BAT" in d]
    if not batteries:
        raise RuntimeError("No battery device was found by upower")
    return batteries[0]


def parse_number(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def parse_duration_minutes(text: Optional[str]) -> Optional[float]:
    """Convert UPower durations such as '2.3 hours' to minutes."""
    value = parse_number(text)
    if value is None or not text:
        return None

    lowered = text.lower()
    if "hour" in lowered:
        return value * 60
    if "minute" in lowered:
        return value
    if "second" in lowered:
        return value / 60
    if "day" in lowered:
        return value * 24 * 60
    return None


class BatteryReader:
    def __init__(self) -> None:
        self.device: Optional[str] = None

    def read(self) -> dict:
        if self.device is None:
            self.device = find_battery_device()

        try:
            text = run_command(["upower", "-i", self.device])
        except (subprocess.SubprocessError, OSError):
            # The battery path can change after suspend/resume or hot-plugging.
            self.device = find_battery_device()
            text = run_command(["upower", "-i", self.device])

        values = {}
        for line in text.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip().lower()] = value.strip()

        left_text = (
            values.get("time to empty")
            or values.get("time to full")
            or "-"
        )

        return {
            "power_w": parse_number(values.get("energy-rate")),
            "battery_pct": parse_number(values.get("percentage")),
            "state": values.get("state", "unknown"),
            "time_left_text": left_text,
            "time_left_min": parse_duration_minutes(left_text),
        }


def read_cpu_counters() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as file:
        values = list(map(int, file.readline().split()[1:]))
    total = sum(values)
    idle = values[3] + values[4]
    return total, idle


def cpu_usage(previous: tuple[int, int], current: tuple[int, int]) -> Optional[float]:
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    return round((1 - idle_delta / total_delta) * 100, 1)


def ram_usage() -> float:
    values = {}
    with open("/proc/meminfo", encoding="utf-8") as file:
        for line in file:
            key, value = line.split(":", 1)
            values[key] = int(value.split()[0])
    return round((1 - values["MemAvailable"] / values["MemTotal"]) * 100, 1)


def format_duration(minutes: float) -> str:
    if minutes >= 24 * 60:
        return f"{minutes / (24 * 60):.1f}d"
    if minutes >= 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes:.0f}m"


def safe_addstr(window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    try:
        window.addnstr(y, max(0, x), text, max(0, width - max(0, x) - 1), attr)
    except curses.error:
        pass


def draw_chart(
    window,
    top: int,
    height: int,
    title: str,
    values: list[Optional[float]],
    formatter,
) -> None:
    rows, cols = window.getmaxyx()
    if height < 4 or top + height > rows:
        return

    safe_addstr(window, top, 0, title, curses.A_BOLD)
    plot_top = top + 1
    plot_height = height - 2
    label_width = 7
    plot_width = max(1, cols - label_width - 2)
    visible = values[-plot_width:]
    numeric = [value for value in visible if value is not None]

    if not numeric:
        safe_addstr(window, plot_top + plot_height // 2, label_width + 2, "No data yet")
        safe_addstr(window, plot_top + plot_height, label_width, "└" + "─" * plot_width)
        return

    minimum = min(numeric)
    maximum = max(numeric)
    if maximum - minimum < 0.01:
        padding = max(abs(maximum) * 0.05, 0.5)
        minimum = max(0.0, minimum - padding)
        maximum += padding

    chart_range = maximum - minimum
    for row in range(plot_height):
        ratio = (plot_height - 1 - row) / max(1, plot_height - 1)
        level = minimum + chart_range * ratio
        safe_addstr(window, plot_top + row, 0, f"{formatter(level):>{label_width - 1}}│")

        for index, value in enumerate(visible):
            if value is None:
                continue
            normalized = (value - minimum) / chart_range
            bar_rows = int(round(normalized * (plot_height - 1))) + 1
            if row >= plot_height - bar_rows:
                safe_addstr(window, plot_top + row, label_width + index, "█")

    safe_addstr(window, plot_top + plot_height, label_width - 1, "└" + "─" * plot_width)


def table_columns(cols: int) -> tuple[str, str]:
    if cols >= 88:
        header = f"{'Time':8} {'Power':>8} {'CPU':>7} {'RAM':>7} {'Batt':>7} {'Remaining':>14}  State"
        separator = "─" * min(cols - 1, 88)
    elif cols >= 66:
        header = f"{'Time':8} {'Power':>8} {'CPU':>7} {'RAM':>7} {'Batt':>7} {'Remaining':>12}"
        separator = "─" * min(cols - 1, 66)
    else:
        header = f"{'Time':8} {'W':>6} {'CPU':>6} {'RAM':>6} {'Bat':>6} {'Left':>8}"
        separator = "─" * max(1, cols - 1)
    return header, separator


def format_sample(sample: Sample, cols: int) -> str:
    power = "-" if sample.power_w is None else f"{sample.power_w:.2f}W"
    cpu = "-" if sample.cpu_pct is None else f"{sample.cpu_pct:.1f}%"
    ram = f"{sample.ram_pct:.1f}%"
    battery = "-" if sample.battery_pct is None else f"{sample.battery_pct:.0f}%"

    if cols >= 88:
        return (
            f"{sample.timestamp:8} {power:>8} {cpu:>7} {ram:>7} "
            f"{battery:>7} {sample.time_left_text:>14}  {sample.state}"
        )
    if cols >= 66:
        return (
            f"{sample.timestamp:8} {power:>8} {cpu:>7} {ram:>7} "
            f"{battery:>7} {sample.time_left_text:>12}"
        )
    return f"{sample.timestamp:8} {power:>6} {cpu:>6} {ram:>6} {battery:>6} {sample.time_left_text:>8}"


def draw_screen(window, samples: deque[Sample], scroll_offset: int, status: str) -> int:
    window.erase()
    rows, cols = window.getmaxyx()

    if rows < 18 or cols < 48:
        safe_addstr(window, 1, 2, "Terminal is too small.", curses.A_BOLD)
        safe_addstr(window, 3, 2, "Minimum recommended size: 48 x 18")
        safe_addstr(window, 5, 2, "Press q to quit.")
        window.refresh()
        return 0

    latest = samples[0] if samples else None
    title = "Battery Monitor"
    if latest:
        summary = (
            f"Power: {latest.power_w:.2f} W" if latest.power_w is not None else "Power: -"
        )
        summary += f"   Battery: {latest.battery_pct:.0f}%" if latest.battery_pct is not None else "   Battery: -"
        summary += f"   Remaining: {latest.time_left_text}   State: {latest.state}"
    else:
        summary = "Collecting first sample..."

    safe_addstr(window, 0, 0, title, curses.A_BOLD)
    safe_addstr(window, 1, 0, summary)
    safe_addstr(window, 2, 0, "q: quit   ↑/↓: scroll table   PgUp/PgDn: page   Home: newest", curses.A_DIM)

    # Reserve at least four table rows. Use taller charts when the terminal allows it.
    available_for_charts = rows - 12
    chart_height = min(PREFERRED_CHART_HEIGHT, max(5, available_for_charts // 2))

    chronological = list(reversed(samples))
    power_values = [sample.power_w for sample in chronological]
    time_values = [sample.time_left_min for sample in chronological]

    first_chart_top = 4
    draw_chart(window, first_chart_top, chart_height, "Power consumption (W)", power_values, lambda v: f"{v:.1f}")

    second_chart_top = first_chart_top + chart_height
    draw_chart(
        window,
        second_chart_top,
        chart_height,
        "Battery time estimate",
        time_values,
        format_duration,
    )

    table_top = second_chart_top + chart_height
    table_rows = max(0, rows - table_top - 4)
    header, separator = table_columns(cols)
    safe_addstr(window, table_top, 0, "Samples — newest row is at the top", curses.A_BOLD)
    safe_addstr(window, table_top + 1, 0, header, curses.A_REVERSE)
    safe_addstr(window, table_top + 2, 0, separator)

    sample_list = list(samples)  # already newest first
    max_offset = max(0, len(sample_list) - table_rows)
    scroll_offset = min(max(0, scroll_offset), max_offset)

    for row_index, sample in enumerate(sample_list[scroll_offset:scroll_offset + table_rows]):
        safe_addstr(window, table_top + 3 + row_index, 0, format_sample(sample, cols))

    footer = f"Every {SAMPLE_INTERVAL:g}s | rows: {len(samples)}/{MAX_SAMPLES} | table offset: {scroll_offset}"
    if status:
        footer += f" | {status}"
    safe_addstr(window, rows - 1, 0, footer, curses.A_DIM)

    window.refresh()
    return max_offset


def main(window) -> None:
    curses.curs_set(0)
    window.keypad(True)
    window.timeout(200)

    battery_reader = BatteryReader()
    samples: deque[Sample] = deque(maxlen=MAX_SAMPLES)
    previous_cpu = read_cpu_counters()
    next_sample_at = 0.0
    scroll_offset = 0
    status = ""

    while True:
        now = time.monotonic()
        if now >= next_sample_at:
            try:
                battery = battery_reader.read()
                current_cpu = read_cpu_counters()
                cpu_pct = cpu_usage(previous_cpu, current_cpu)
                previous_cpu = current_cpu

                samples.appendleft(
                    Sample(
                        timestamp=time.strftime("%H:%M:%S"),
                        power_w=battery["power_w"],
                        cpu_pct=cpu_pct,
                        ram_pct=ram_usage(),
                        battery_pct=battery["battery_pct"],
                        time_left_min=battery["time_left_min"],
                        time_left_text=battery["time_left_text"],
                        state=battery["state"],
                    )
                )
                status = ""
            except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as error:
                status = str(error)

            # Keep a stable interval even if one command was slow.
            next_sample_at = now + SAMPLE_INTERVAL

        max_offset = draw_screen(window, samples, scroll_offset, status)
        key = window.getch()

        if key in (ord("q"), ord("Q"), 27):
            break
        if key == curses.KEY_UP:
            scroll_offset = min(max_offset, scroll_offset + 1)
        elif key == curses.KEY_DOWN:
            scroll_offset = max(0, scroll_offset - 1)
        elif key == curses.KEY_PPAGE:
            scroll_offset = min(max_offset, scroll_offset + 10)
        elif key == curses.KEY_NPAGE:
            scroll_offset = max(0, scroll_offset - 10)
        elif key in (curses.KEY_HOME, ord("g")):
            scroll_offset = 0
        elif key == curses.KEY_END:
            scroll_offset = max_offset


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
