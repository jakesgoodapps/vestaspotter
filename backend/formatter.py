from dataclasses import dataclass
from typing import Optional

from .airline_colors import GREEN, RED, WHITE, YELLOW

ROWS = 6
COLS = 22

# Vestaboard character code map. Unknown chars render as blank.
CHAR_MAP: dict[str, int] = {
    " ": 0,
    **{chr(ord("A") + i): i + 1 for i in range(26)},
    "1": 27, "2": 28, "3": 29, "4": 30, "5": 31,
    "6": 32, "7": 33, "8": 34, "9": 35, "0": 36,
    "!": 37, "@": 38, "#": 39, "$": 40,
    "(": 41, ")": 42, "-": 44, "+": 46, "&": 47,
    "=": 48, ";": 49, ":": 50, "'": 52, '"': 53,
    "%": 54, ",": 55, ".": 56, "/": 59, "?": 60,
    "°": 62,
}
CODE_TO_CHAR: dict[int, str] = {v: k for k, v in CHAR_MAP.items()}

COLOR_TILE_CODES = {63, 64, 65, 66, 67, 68, 69, 70, 71}


@dataclass
class AircraftView:
    airline_iata: str
    flight_number: int
    airline_color: int
    origin_iata: str
    destination_iata: str
    scheduled_departure: str
    actual_departure: str
    departure_delay_min: int
    scheduled_arrival: str
    estimated_arrival: str
    arrival_delay_min: int
    tail_number: str
    year_built: Optional[int]
    sighting_count: int
    aircraft_name: str
    is_rare: bool = False
    rare_reason: Optional[str] = None
    livery_name: Optional[str] = None
    is_king: bool = False  # tail has the highest sighting count in our DB


@dataclass
class AirportView:
    iata: str
    status_color: int
    arrivals_today: int
    departures_today: int


def text_to_codes(text: str) -> list[int]:
    return [CHAR_MAP.get(c.upper(), 0) for c in text]


def write_text(row: list[int], start: int, text: str) -> None:
    for i, code in enumerate(text_to_codes(text)):
        pos = start + i
        if 0 <= pos < COLS:
            row[pos] = code


def blank_row() -> list[int]:
    return [0] * COLS


def _delay_tile_and_text(delay_min: Optional[int]) -> tuple[int, str]:
    """Map delay magnitude → (tile_color, delay_text) for the row 2/3 right cluster.

      ≤ 0   → GREEN  + "OT"          (on time or early)
      1-14  → YELLOW + "+Xm"         (mild delay)
      15+   → RED    + "+Xm"         (significant delay)
    """
    if delay_min is None or delay_min <= 0:
        return (GREEN, "OT")
    if delay_min < 15:
        return (YELLOW, f"+{delay_min}M")
    return (RED, f"+{delay_min}M")


def _format_time_row(label: str, sch: str, act: str, delay_min: Optional[int]) -> list[int]:
    """Compose 'LBL  SCH // ACT  [tile][delay]' into 22 chars.

    Tries progressively tighter spacing until it fits — handles the worst case
    of 5-char times + 5-char delays (+131M) by dropping spacing around //.
    """
    row = blank_row()
    tile, delay_text = _delay_tile_and_text(delay_min)
    right_len = 1 + len(delay_text)  # tile cell + delay text

    layouts = [
        f"{label}  {sch} // {act}",   # roomy: 2 spaces after label
        f"{label} {sch} // {act}",    # 1 space after label
        f"{label} {sch}//{act}",      # compact slashes, no spaces around
        f"{label} {sch} {act}",       # drop slashes entirely
    ]
    for left in layouts:
        if len(left) + 1 + right_len <= COLS:
            break

    write_text(row, 0, left)
    tile_pos = COLS - right_len
    row[tile_pos] = tile
    write_text(row, tile_pos + 1, delay_text)
    return row


def format_row_1_flight(view: AircraftView) -> list[int]:
    """Row 1: [tile]CARRIER FLIGHTNUM   ORIG -- DEST"""
    row = blank_row()
    row[0] = view.airline_color
    write_text(row, 1, f"{view.airline_iata} {view.flight_number}")
    route = f"{view.origin_iata} -- {view.destination_iata}"
    write_text(row, COLS - len(route), route)
    return row


def format_row_2_dep(view: AircraftView) -> list[int]:
    """Row 2: DEP  SCH // ACT  [color tile][delay]"""
    return _format_time_row(
        "DEP", view.scheduled_departure, view.actual_departure, view.departure_delay_min,
    )


def format_row_3_arr(view: AircraftView) -> list[int]:
    """Row 3: ARR  SCH // EST  [color tile][delay]"""
    return _format_time_row(
        "ARR", view.scheduled_arrival, view.estimated_arrival, view.arrival_delay_min,
    )


def format_row_4_tail(view: AircraftView) -> list[int]:
    """Row 4: [crown?] TAIL    YEAR    NX SEEN

    If this tail has the highest sighting count in our DB (king of the hill),
    a yellow tile crown is placed at column 0 and the tail shifts right by one.
    """
    row = blank_row()
    tail_start = 0
    if view.is_king:
        row[0] = YELLOW
        tail_start = 1
    write_text(row, tail_start, view.tail_number)
    if view.year_built:
        year_str = str(view.year_built)
        write_text(row, 9, year_str)
    sighting = f"{view.sighting_count}X SEEN"
    write_text(row, COLS - len(sighting), sighting)
    return row


def format_row_5_type(view: AircraftView) -> list[int]:
    """Row 5: bookends + aircraft type / rare / livery callout, centered.

    The bookend color IS the special-flag signal (yellow = rare/livery, white =
    normal). When text is too long to fit with bookends, the "RARE:"/"LIVERY:"
    prefix words are dropped — the colored bookends carry the meaning on their own.
    """
    row = blank_row()

    if view.livery_name:
        bookend = YELLOW
        full_text = f"LIVERY: {view.livery_name}".upper()
        fallback_text = view.livery_name.upper()
    elif view.is_rare:
        bookend = YELLOW
        rare_label = (view.rare_reason or view.aircraft_name).upper()
        full_text = f"RARE: {rare_label}"
        fallback_text = rare_label
    else:
        bookend = WHITE
        full_text = view.aircraft_name.upper()
        fallback_text = full_text

    # Try full_text with 2 bookends, then 1; if still too long, drop the prefix.
    for candidate in (full_text, fallback_text):
        if len(candidate) <= COLS - 4:
            text, n_bookends = candidate, 2
            break
        if len(candidate) <= COLS - 2:
            text, n_bookends = candidate, 1
            break
    else:
        # Even fallback is too long — truncate to fit with 1 bookend each side.
        text = fallback_text[: COLS - 2]
        n_bookends = 1

    for i in range(n_bookends):
        row[i] = bookend
        row[COLS - 1 - i] = bookend

    inner_start = n_bookends
    inner_width = COLS - (2 * n_bookends)
    pad_left = (inner_width - len(text)) // 2
    write_text(row, inner_start + pad_left, text)
    return row


def format_row_6_airport(airport: AirportView) -> list[int]:
    """Row 6: [tile]DCA NN ARR / NN DEP

    Both numbers are MY personal counts of flights I've pushed to the board
    today (arriving at / departing from the airport). Free — pure SQL.
    Status tile color comes from FA's airport delays endpoint.
    """
    row = blank_row()
    row[0] = airport.status_color
    body = f"{airport.iata} {airport.arrivals_today} ARR / {airport.departures_today} DEP"
    write_text(row, 1, body)
    return row


def format_board(aircraft: AircraftView, airport: AirportView) -> list[list[int]]:
    return [
        format_row_1_flight(aircraft),
        format_row_2_dep(aircraft),
        format_row_3_arr(aircraft),
        format_row_4_tail(aircraft),
        format_row_5_type(aircraft),
        format_row_6_airport(airport),
    ]


def format_no_traffic_board(airport: AirportView) -> list[list[int]]:
    """Sparse board for when nothing is overhead. Centered 'NO TRAFFIC' message,
    airport status footer."""
    board = [blank_row() for _ in range(ROWS)]
    text = "NO TRAFFIC OVERHEAD"
    pad_left = (COLS - len(text)) // 2
    write_text(board[2], pad_left, text)
    board[5] = format_row_6_airport(airport)
    return board


def _full_color_row(color: int) -> list[int]:
    return [color] * COLS


def _flag_stripe_row() -> list[int]:
    """Red 7 / White 8 / Blue 7 — three flag-style block stripes across 22 cells."""
    RED, WHITE, BLUE = 63, 69, 67
    return [RED] * 7 + [WHITE] * 8 + [BLUE] * 7


def _centered_text_row(text: str) -> list[int]:
    row = blank_row()
    text = text[:COLS]
    pad_left = (COLS - len(text)) // 2
    write_text(row, pad_left, text)
    return row


def format_potus_confirmed_board(
    title: str = "POTUS HEADS UP",
    line2: str = "MOVEMENT 30-40 MIN",
    line3: str = "",
    footer: str = "WATCH THE WINDOW",
) -> list[list[int]]:
    """POTUS heads-up board (CONFIRMED) with red/white/blue flag stripes top + bottom.
    All four text rows are configurable so we can swap in drill-mode wording."""
    return [
        _flag_stripe_row(),
        _centered_text_row(title),
        _centered_text_row(line2),
        _centered_text_row(line3),
        _centered_text_row(footer),
        _flag_stripe_row(),
    ]


def format_potus_imminent_board(
    title: str = "POTUS IMMINENT",
    line2: str = "MOVEMENT IMMINENT",
    line3: str = "",
    footer: str = "LOOK NOW",
) -> list[list[int]]:
    """POTUS imminent board (IMMINENT) — same flag stripes; text fully configurable
    so drill-end mode can swap in 'goodbye helo' wording."""
    return [
        _flag_stripe_row(),
        _centered_text_row(title),
        _centered_text_row(line2),
        _centered_text_row(line3),
        _centered_text_row(footer),
        _flag_stripe_row(),
    ]


COLOR_SYMBOLS = {63: "🟥", 64: "🟧", 65: "🟨", 66: "🟩", 67: "🟦", 68: "🟪", 69: "⬜", 70: "⬛", 71: "🟫"}


def render_ascii(matrix: list[list[int]]) -> str:
    """Render a 6x22 matrix as terminal-friendly preview."""
    lines = []
    for row in matrix:
        line = ""
        for code in row:
            if code in COLOR_SYMBOLS:
                line += COLOR_SYMBOLS[code]
            elif code == 0:
                line += " "
            else:
                line += CODE_TO_CHAR.get(code, "?")
        lines.append(line)
    return "\n".join(lines)
