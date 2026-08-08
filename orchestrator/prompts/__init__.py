"""Prompt templates shipped as package data (origin R20).

Templates use ``string.Template`` ``$placeholders`` — not str.format — so JSON
examples with braces stay literal.
"""

from importlib.resources import files


def load_template(name: str) -> str:
    return (files("orchestrator.prompts") / f"{name}.md").read_text()
