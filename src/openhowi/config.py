"""Load a cutting job from a TOML file (see ``examples/``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .airfoil import AirfoilError, load_airfoil
from .machine import Machine
from .toolpath import Placement
from .wing import WingPanel


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WingJob:
    title: str
    machine: Machine
    wing: WingPanel
    placement: Placement


def _build(cls: type, table: dict[str, Any], section: str) -> Any:
    allowed = {f.name for f in fields(cls)}
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")
    try:
        return cls(**table)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{section}] {exc}") from exc


def _table(data: dict[str, Any], name: str, required: bool) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        if required:
            raise ConfigError(f"missing [{name}] section")
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return dict(value)


def load_wing_job(path: str | Path) -> WingJob:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    unknown = sorted(set(data) - {"title", "machine", "wing", "placement"})
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(unknown)}")

    wing_table = _table(data, "wing", required=True)
    for key in ("root_airfoil", "tip_airfoil"):
        spec = wing_table.get(key)
        if spec is None:
            continue
        if not isinstance(spec, str):
            raise ConfigError(f'[wing] {key} must be a string such as "naca2412" or "clarky.dat"')
        try:
            wing_table[key] = load_airfoil(spec, base_dir=path.parent)
        except AirfoilError as exc:
            raise ConfigError(f"[wing] {key}: {exc}") from exc

    return WingJob(
        title=str(data.get("title", path.stem)),
        machine=_build(Machine, _table(data, "machine", required=False), "machine"),
        wing=_build(WingPanel, wing_table, "wing"),
        placement=_build(Placement, _table(data, "placement", required=False), "placement"),
    )
