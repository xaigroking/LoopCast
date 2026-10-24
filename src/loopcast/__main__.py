"""Command-line entry point for LoopCast."""

import argparse
from importlib.metadata import version


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="loopcast",
        description=(
            "LoopCast application scaffold. "
            "Video generation and publishing are not implemented yet."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('loopcast')}")
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()
