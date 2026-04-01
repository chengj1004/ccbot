"""Application entry point — CLI dispatcher and bot bootstrap.

Handles three execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. `ccbot wecom` — starts the WeCom (企业微信) bot with webhook server.
  3. Default — configures logging, initializes tmux session, and starts the
     Telegram bot polling loop via bot.create_bot().
"""

import logging
import sys


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hook import hook_main

        hook_main()
        return

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    if len(sys.argv) > 1 and sys.argv[1] == "wecom":
        _run_wecom()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "wecom-bot":
        _run_wecom_bot()
        return

    _run_telegram()


def _run_telegram() -> None:
    """Start the Telegram bot."""
    try:
        from .config import config

        config.validate_telegram()
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot()
    application.run_polling(allowed_updates=["message", "callback_query"])


def _run_wecom() -> None:
    """Start the WeCom bot."""
    # WeComConfig must be created BEFORE importing shared config,
    # because Config.__init__ scrubs WECOM_* sensitive env vars.
    try:
        from .wecom.config import WeComConfig

        wecom_config = WeComConfig()
        wecom_config.validate()
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Add WeCom config to {env_path}:\n")
        print("  WECOM_CORP_ID=your_corp_id")
        print("  WECOM_SECRET=your_app_secret")
        print("  WECOM_AGENT_ID=your_agent_id")
        print("  WECOM_CALLBACK_TOKEN=your_callback_token")
        print("  WECOM_ENCODING_AES_KEY=your_encoding_aes_key")
        sys.exit(1)

    # Now safe to init shared config (env vars already captured above)
    try:
        from .config import config  # noqa: F841 — triggers shared config init
    except ValueError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting WeCom bot...")
    from .wecom.bot import run_wecom_bot

    run_wecom_bot(wecom_config)


def _run_wecom_bot() -> None:
    """Start the WeCom AI Bot (智能机器人 WebSocket mode)."""
    # WeComConfig must be created BEFORE importing shared config,
    # because Config.__init__ scrubs WECOM_* sensitive env vars.
    try:
        from .wecom.config import WeComConfig

        wecom_config = WeComConfig()
        wecom_config.validate_bot()
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Add WeCom AI Bot config to {env_path}:\n")
        print("  WECOM_BOT_ID=your_bot_id")
        print("  WECOM_BOT_SECRET=your_bot_secret")
        print()
        print("Optional (for media download):")
        print("  WECOM_CORP_ID=your_corp_id")
        print("  WECOM_SECRET=your_corp_secret")
        sys.exit(1)

    # Now safe to init shared config (env vars already captured above)
    try:
        from .config import config  # noqa: F841 — triggers shared config init
    except ValueError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting WeCom AI Bot (WebSocket mode)...")
    from .wecom.aibot import run_wecom_aibot

    run_wecom_aibot(wecom_config)


if __name__ == "__main__":
    main()
