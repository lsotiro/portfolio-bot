"""portfolio-bot — boilerplate entry point."""

from __future__ import annotations


def greet(name: str) -> str:
    return f"Hello, {name}! Welcome to portfolio-bot."


def main() -> None:
    print(greet("world"))


if __name__ == "__main__":
    main()
