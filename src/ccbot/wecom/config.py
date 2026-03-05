"""WeCom-specific configuration.

Loads corp_id, secret, agent_id, callback token/encoding_aes_key from
environment variables. Group-to-directory mappings are loaded from
wecom_groups.json in the config directory.

Key class: WeComConfig.
"""

import json
import logging
import os
from dataclasses import dataclass

from ..utils import atomic_write_json, ccbot_dir

logger = logging.getLogger(__name__)


@dataclass
class GroupBinding:
    """A WeCom group chat binding to a working directory."""

    cwd: str
    name: str = ""
    window_id: str = ""  # Filled at runtime when tmux window is created
    verbose: bool = False  # When True, send tool_use/tool_result summaries


class WeComConfig:
    """WeCom application configuration."""

    def __init__(self) -> None:
        self.config_dir = ccbot_dir()

        # WeCom API credentials
        self.corp_id: str = os.getenv("WECOM_CORP_ID", "")
        self.secret: str = os.getenv("WECOM_SECRET", "")
        self.agent_id: int = int(os.getenv("WECOM_AGENT_ID", "0"))

        # Callback verification
        self.callback_token: str = os.getenv("WECOM_CALLBACK_TOKEN", "")
        self.encoding_aes_key: str = os.getenv("WECOM_ENCODING_AES_KEY", "")

        # Webhook server
        self.listen_host: str = os.getenv("WECOM_LISTEN_HOST", "0.0.0.0")
        self.listen_port: int = int(os.getenv("WECOM_LISTEN_PORT", "8080"))

        # Allowed users (optional, comma-separated WeCom userids)
        allowed_str = os.getenv("WECOM_ALLOWED_USERS", "")
        self.allowed_users: set[str] = {
            u.strip() for u in allowed_str.split(",") if u.strip()
        }

        # State files
        self.groups_file = self.config_dir / "wecom_groups.json"

        # Group bindings loaded from file
        self.groups: dict[str, GroupBinding] = {}
        self._load_groups()

    def _load_groups(self) -> None:
        """Load group bindings from wecom_groups.json."""
        if not self.groups_file.exists():
            return
        try:
            data = json.loads(self.groups_file.read_text())
            for chat_id, info in data.items():
                self.groups[chat_id] = GroupBinding(
                    cwd=info.get("cwd", ""),
                    name=info.get("name", ""),
                    verbose=info.get("verbose", False),
                )
            logger.info("Loaded %d WeCom group bindings", len(self.groups))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load wecom_groups.json: %s", e)

    def save_groups(self) -> None:
        """Persist group bindings to wecom_groups.json."""
        data = {}
        for chat_id, binding in self.groups.items():
            entry: dict[str, str | bool] = {"cwd": binding.cwd}
            if binding.name:
                entry["name"] = binding.name
            if binding.verbose:
                entry["verbose"] = binding.verbose
            data[chat_id] = entry
        atomic_write_json(self.groups_file, data)
        logger.debug("Saved %d group bindings", len(data))

    def is_user_allowed(self, userid: str) -> bool:
        """Check if a user is allowed. Empty allowed_users means allow all."""
        if not self.allowed_users:
            return True
        return userid in self.allowed_users

    def validate(self) -> None:
        """Validate required config fields."""
        missing = []
        if not self.corp_id:
            missing.append("WECOM_CORP_ID")
        if not self.secret:
            missing.append("WECOM_SECRET")
        if not self.agent_id:
            missing.append("WECOM_AGENT_ID")
        if not self.callback_token:
            missing.append("WECOM_CALLBACK_TOKEN")
        if not self.encoding_aes_key:
            missing.append("WECOM_ENCODING_AES_KEY")
        if missing:
            raise ValueError(f"Missing required WeCom config: {', '.join(missing)}")
