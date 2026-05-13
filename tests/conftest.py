"""
conftest.py — shared pytest configuration for export-slack tests.
"""

def pytest_addoption(parser):
    parser.addoption(
        "--export-dir",
        action="append",
        default=[],
        metavar="PATH",
        help="Path to an exported channel directory. May be repeated.",
    )
