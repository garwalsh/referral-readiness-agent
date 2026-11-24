#!/usr/bin/env python3
"""Django management entry point for the RRR dashboard."""

import os
import sys

from django.core.management import execute_from_command_line


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dashboard.settings")
    execute_from_command_line(sys.argv)
