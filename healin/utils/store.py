import copy
import json
import os


def _fill(target: dict, defaults: dict) -> None:
    """Add any missing keys from defaults (recursively) without touching existing values."""
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _fill(target[key], value)


class GuildStore:
    """Tiny per-server settings store backed by one JSON file."""

    def __init__(self, name: str, defaults: dict):
        self.dir = os.getenv("DATA_DIR", "data")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, f"{name}.json")
        self.defaults = defaults
        self.data: dict = {}

        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                # keep the broken file around instead of overwriting it
                try:
                    os.replace(self.path, self.path + ".corrupt")
                except OSError:
                    pass
                self.data = {}

    def get(self, guild_id: int) -> dict:
        cfg = self.data.setdefault(str(guild_id), {})
        _fill(cfg, self.defaults)
        return cfg

    def reset(self, guild_id: int) -> dict:
        self.data[str(guild_id)] = copy.deepcopy(self.defaults)
        self.save()
        return self.data[str(guild_id)]

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)
