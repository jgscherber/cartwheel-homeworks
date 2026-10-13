"""Record HW1 conversation drafts using the existing authenticated CLI.

Usage:
    uv run python -m agent.hw1_cli --role shopper --user 1
    uv run python -m agent.hw1_cli --role merchant --user 9002
    uv run python -m agent.hw1_cli --role support --user 9501 --output my-session.jsonl
"""

from pathlib import Path

from agent.cli import main as chat_main


def main() -> None:
    chat_main(default_output=Path("hw1-session.jsonl"))


if __name__ == "__main__":
    main()
