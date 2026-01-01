import argparse
import logging

from .ai_operator import run_ai_loop


def parse_args():
    parser = argparse.ArgumentParser(description="AI operator for DALI lamp control.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without sending commands to hardware; logs intended actions only.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_ai_loop(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
