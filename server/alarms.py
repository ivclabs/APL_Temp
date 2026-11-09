"""Alarm rules: decide if a new temperature should raise an alarm.

Kept separate from the web code so the rules are easy to read and test.
"""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class AlarmConfig:
    high_c: float = 28.0            # "high" alarm above this
    low_c: float = 15.0             # "low" alarm below this
    rise_c_per_min: float = 1.0     # "rising" alarm at this speed or faster
    rise_window_min: float = 5.0    # how far back we look to measure the speed
    rise_min_age_min: float = 1.0   # compare only with readings at least this old
    cooldown_min: float = 15.0      # the same alarm at most once per this many minutes

    @classmethod
    def from_env(cls):
        """Read limits from .env so they can change without touching code."""
        def num(name, default):
            return float(os.environ.get(name, default))

        return cls(
            high_c=num("ALARM_HIGH_C", cls.high_c),
            low_c=num("ALARM_LOW_C", cls.low_c),
            rise_c_per_min=num("ALARM_RISE_C_PER_MIN", cls.rise_c_per_min),
            rise_window_min=num("ALARM_RISE_WINDOW_MIN", cls.rise_window_min),
            rise_min_age_min=num("ALARM_RISE_MIN_AGE_MIN", cls.rise_min_age_min),
            cooldown_min=num("ALARM_COOLDOWN_MIN", cls.cooldown_min),
        )


def rise_rate(temp_c, now, history, cfg):
    """Fastest rise in °C per minute compared with earlier readings.

    history: list of (time, temp_c) from the last few minutes.
    Returns None if there is no reading old enough to compare with.
    We take the fastest rise so a sudden jump is not hidden by a calm period before it.
    """
    rates = []
    for old_time, old_temp in history:
        minutes = (now - old_time).total_seconds() / 60
        if minutes >= cfg.rise_min_age_min:
            rates.append((temp_c - old_temp) / minutes)
    return max(rates) if rates else None


def check_rules(temp_c, now, history, cfg):
    """Return a list of (kind, message) for every rule this reading breaks."""
    alarms = []

    if temp_c > cfg.high_c:
        alarms.append(("high", f"Temperature {temp_c:.1f} °C is above the limit of {cfg.high_c:.1f} °C"))

    if temp_c < cfg.low_c:
        alarms.append(("low", f"Temperature {temp_c:.1f} °C is below the limit of {cfg.low_c:.1f} °C"))

    rate = rise_rate(temp_c, now, history, cfg)
    if rate is not None and rate >= cfg.rise_c_per_min:
        alarms.append(("rising", f"Temperature is rising {rate:.1f} °C per minute "
                                 f"(limit {cfg.rise_c_per_min:.1f} °C/min), now {temp_c:.1f} °C"))

    return alarms


def cooldown_over(last_alarm_time, now, cfg):
    """True if enough time has passed since the last alarm of the same kind."""
    if last_alarm_time is None:
        return True
    return now - last_alarm_time >= timedelta(minutes=cfg.cooldown_min)


def parse_time(text):
    return datetime.fromisoformat(text)
