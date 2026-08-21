"""Entry point: ``python -m dsbot``."""

from __future__ import annotations

import logging
import sys

import discord

from .bot import build_bot
from .config import Config


def main() -> int:
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    if not cfg.token:
        print("DISCORD_TOKEN is not set (see .env.example)", file=sys.stderr)
        return 2

    try:
        bot = build_bot(cfg)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        bot.run(cfg.token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected the token", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
