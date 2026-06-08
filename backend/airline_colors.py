RED = 63
ORANGE = 64
YELLOW = 65
GREEN = 66
BLUE = 67
VIOLET = 68
WHITE = 69
BLACK = 70
FILLED = 71

DEFAULT_COLOR = WHITE

AIRLINE_COLORS: dict[str, int] = {
    "AA": RED,        # American
    "UA": BLUE,       # United
    "DL": RED,        # Delta (red triangle widget)
    "WN": ORANGE,     # Southwest
    "B6": BLUE,       # JetBlue
    "AS": GREEN,      # Alaska (dark green)
    "F9": GREEN,      # Frontier
    "NK": YELLOW,     # Spirit
    "G4": ORANGE,     # Allegiant
    "SY": ORANGE,     # Sun Country
    "HA": VIOLET,     # Hawaiian
    "AC": RED,        # Air Canada
    "WS": BLUE,       # WestJet
    # Regional carriers — color-matched to their mainline partner
    "9E": RED,        # Endeavor (Delta)
    "OH": RED,        # PSA (American)
    "MQ": RED,        # Envoy (American)
    "OO": BLUE,       # SkyWest (mixed; default blue)
    "YX": BLUE,       # Republic (mixed)
    "ZW": BLUE,       # Air Wisconsin (United)
    "C5": BLUE,       # CommuteAir (United)
    "QX": GREEN,      # Horizon (Alaska)
    "G7": ORANGE,     # GoJet
    "EM": RED,        # Empire (American)
}


def color_for(airline_iata: str | None) -> int:
    if not airline_iata:
        return DEFAULT_COLOR
    return AIRLINE_COLORS.get(airline_iata.upper(), DEFAULT_COLOR)
