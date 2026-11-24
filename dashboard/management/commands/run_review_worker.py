"""Consume SQLite review jobs in a separately supervised process."""

from time import sleep

from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from dashboard.worker import claim_next, execute_run


class Command(BaseCommand):
    help = "Process queued agent reviews without blocking web requests."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--once", action="store_true", help="Process at most one queued review.")

    def handle(self, *args, **options) -> None:
        while True:
            close_old_connections()
            try:
                run = claim_next()
                if run is not None:
                    execute_run(run)
            except OperationalError:
                if options["once"]:
                    raise
                self.stderr.write("The run database is temporarily unavailable. Retrying.")
                run = None
            if options["once"]:
                return
            if run is None:
                sleep(1)
