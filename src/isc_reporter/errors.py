"""Errors that stop a command with a readable message."""


class UsageError(Exception):
    """Bad input file, bad arguments or missing configuration. Aborts the run."""
