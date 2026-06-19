"""Entry point: wire everything together and run the Socket Mode loop.

Single long-running process holding the Slack connection, the scheduler, the DB
handle, and the scoring logic. No queue, no broker, no public HTTP endpoint.
"""

from __future__ import annotations

import logging
import signal
import sys

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .claude_client import ClaudeClient
from .config import load_config
from .db import open_database
from .poll import PollService
from .scheduler import Scheduler
from .slack_app import create_app, register_handlers


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("lunchbot")

    config = load_config()
    config.require()

    db = open_database(config.db_path)
    claude = ClaudeClient(config.anthropic_api_key, config.anthropic_model)
    scheduler = Scheduler(config)

    app = create_app(config)

    poll = PollService(
        db=db,
        config=config,
        slack_client=app.client,
        claude=claude,
        schedule_close=scheduler.schedule_close,
    )
    scheduler.attach(poll)
    register_handlers(app, poll)

    scheduler.start()
    poll.recover_open_polls()  # restart recovery (spec section 12)

    handler = SocketModeHandler(app, config.slack_app_token)

    def shutdown(*_):
        log.info("Shutting down...")
        scheduler.shutdown()
        db.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Lunch Bot is up. Channel=%s, Claude=%s", config.channel_id, claude.enabled)
    handler.start()


if __name__ == "__main__":
    main()
