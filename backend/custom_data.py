"""Loads the YAML-curated rare types + livery list into memory at import time.

Both files are small and read-only at runtime — load once, look up O(1).
"""
import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

from .config import settings as _custom_data_settings

_HERE = Path(__file__).resolve().parent
_LIVERIES_PATH = _HERE / "liveries.yaml"
# airport_rare YAML is per-airport — file is selected by configured AIRPORT_CODE.
# E.g., AIRPORT_CODE=DCA → airport_rare_DCA.yaml. If the file doesn't exist for
# the user's airport, the rare-by-airport feature simply does nothing.
_AIRPORT_RARE_PATH = _HERE / f"airport_rare_{_custom_data_settings.airport_code.upper()}.yaml"
_AIRCRAFT_NAMES_PATH = _HERE / "aircraft_names.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        logger.warning("%s missing, using empty map", path.name)
        return {}
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        return {str(k).upper(): str(v) for k, v in data.items()}
    except Exception as e:
        logger.error("failed to load %s: %s", path.name, e)
        return {}


_LIVERIES: dict[str, str] = _load_yaml(_LIVERIES_PATH)
_AIRPORT_RARE: dict[str, str] = _load_yaml(_AIRPORT_RARE_PATH)
_AIRCRAFT_NAMES: dict[str, str] = _load_yaml(_AIRCRAFT_NAMES_PATH)


def get_livery(registration: Optional[str]) -> Optional[str]:
    if not registration:
        return None
    return _LIVERIES.get(registration.upper().replace("-", "").replace(" ", ""))


def get_airport_rare(aircraft_type: Optional[str]) -> Optional[str]:
    """Return human label if this ICAO type is rare for the local airport."""
    if not aircraft_type:
        return None
    return _AIRPORT_RARE.get(aircraft_type.upper())


def get_aircraft_name(aircraft_type: Optional[str]) -> Optional[str]:
    """Return our preferred short display name for an ICAO type code, or None
    if no override exists (caller should fall back to FA's aircraft_name)."""
    if not aircraft_type:
        return None
    return _AIRCRAFT_NAMES.get(aircraft_type.upper())
