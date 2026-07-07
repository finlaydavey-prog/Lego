"""Configuration: config.yaml + selectors.yaml for tunables, .env for secrets."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else PROJECT_ROOT
        load_dotenv(self.root / ".env")
        with open(self.root / "config.yaml") as f:
            self.data: dict = yaml.safe_load(f)
        with open(self.root / "selectors.yaml") as f:
            self.selectors: dict = yaml.safe_load(f)

    def get(self, dotted: str, default=None):
        """config.get("sources.gumtree.poll_minutes")"""
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @staticmethod
    def env(name: str, default: str | None = None) -> str | None:
        val = os.environ.get(name, default)
        return val if val else default

    def path(self, dotted: str, default: str) -> Path:
        p = Path(self.get(dotted, default))
        return p if p.is_absolute() else self.root / p
