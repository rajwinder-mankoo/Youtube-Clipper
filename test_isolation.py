"""Compatibility entry point for the regression-check suite."""

from runpy import run_module


if __name__ == "__main__":
    run_module("tests.test_isolation", run_name="__main__")
