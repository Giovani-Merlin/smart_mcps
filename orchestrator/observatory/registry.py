"""The project registry: one YAML file naming every repo the Observatory watches.

One registry spans all projects, which is the point of R19 — a single running app
serves runs from several repos without a restart. It lives at
``~/.orchestrator-ui.yaml`` by default and is overridable with ``--registry`` so
tests can point at a ``tmp_path`` file instead of touching ``$HOME``.

A registry that is missing, empty, or malformed yields an empty project list with
a message rather than a crash: the Observatory is often the first thing an
operator launches, before any registry exists.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

REGISTRY_FILENAME = "~/.orchestrator-ui.yaml"


class Project(BaseModel):
    """One registered repo. ``error`` is set — rather than the entry dropped —
    when the repo path is unusable, so a typo in the registry is visible in the
    UI instead of silently shortening the list."""

    name: str
    repo: str
    error: str | None = None

    @property
    def repo_path(self) -> Path:
        return Path(self.repo).expanduser()

    @property
    def usable(self) -> bool:
        return self.error is None


def default_registry_path() -> Path:
    return Path(REGISTRY_FILENAME).expanduser()


def load_registry(path: Path | None, fallback_repo: Path | None = None) -> list[Project]:
    """Projects in file order. ``fallback_repo`` is used only when no registry
    file exists — it is what makes ``smart-mcps-orchestrate ui`` in a repo work
    with zero configuration, without inventing entries for a registry that does
    exist but is empty."""
    if path is not None and path.is_file():
        return _parse(path)
    if fallback_repo is not None:
        return [_validated(Project(name=fallback_repo.name, repo=str(fallback_repo)))]
    return []


def _parse(path: Path) -> list[Project]:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [Project(name=str(path), repo="", error=f"registry is not valid YAML: {exc}")]

    # Both shapes are accepted: a mapping with a `projects:` key (documented) and
    # a bare top-level list (what people write from memory).
    entries = raw.get("projects", []) if isinstance(raw, dict) else raw
    if not entries:
        return []
    if not isinstance(entries, list):
        return [Project(name=str(path), repo="", error="registry `projects` must be a list")]

    projects: list[Project] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "repo" not in entry:
            projects.append(
                Project(
                    name=str(entry)[:60] or f"entry {index}",
                    repo="",
                    error="registry entry needs `name` and `repo` keys",
                )
            )
            continue
        repo = str(entry["repo"])
        name = str(entry.get("name") or Path(repo).expanduser().name)
        projects.append(_validated(Project(name=name, repo=repo)))
    return projects


def _validated(project: Project) -> Project:
    path = project.repo_path
    if not path.exists():
        return project.model_copy(update={"error": f"repo path does not exist: {path}"})
    if not path.is_dir():
        return project.model_copy(update={"error": f"repo path is not a directory: {path}"})
    return project


def find_project(projects: list[Project], name: str) -> Project | None:
    for project in projects:
        if project.name == name:
            return project
    return None
