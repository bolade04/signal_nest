"""Inert negative-control fixture for the SAST gate. Never imported or executed."""

import subprocess


def negative_control(user_input: str) -> int:
    return subprocess.call(user_input, shell=True)
