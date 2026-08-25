"""Render sample-data scenarios to terminal ASCII. Run for visual layout check.

  python -m backend.render_test
"""
from .formatter import format_board, render_ascii
from . import sample_data as sd


SCENARIOS = [
    ("NORMAL FLIGHT (UA 737, on-time airport)", sd.normal_united, sd.airport_ontime),
    ("RARE TYPE — 757 callout (yellow bookends)", sd.rare_757, sd.airport_ontime),
    ("CUSTOM LIVERY — AA Astrojet (yellow bookends)", sd.livery_aa_astrojet, sd.airport_ontime),
    ("DELAYED FLIGHT (Southwest +35m, yellow airport)", sd.delayed_southwest, sd.airport_minor_delays),
    ("RARE WIDEBODY (LH A330 from FRA, red airport)", sd.regional_widebody_rare, sd.airport_major_delays),
]


def _card_scenarios() -> list[tuple[str, list[list[int]]]]:
    """Idle-deck cards rendered from canned data (no DB needed)."""
    from datetime import date
    from . import idle_cards, sun_times
    from .formatter import format_row_6_airport

    today = date(2026, 8, 24)
    footer = format_row_6_airport(sd.airport_ontime())
    specials = [
        {"label": "757 FROM DEN", "direction": "DEP", "sched_local": "2026-08-24T10:05:00-04:00", "seen_at": "x"},
        {"label": "AA ASTROJET RETRO", "direction": "ARR", "sched_local": "2026-08-24T15:15:00-04:00", "seen_at": None},
        {"label": "TWA HERITAGE", "direction": "DEP", "sched_local": "2026-08-24T21:40:00-04:00", "seen_at": None},
    ]
    yesterday = {"date": "2026-08-23", "total_flights": 324, "unique_tails": 201, "top_tail": "N33PP",
                 "top_tail_count": 4, "top_type": "CRJ7", "top_type_count": 61,
                 "hourly": [0] * 17 + [31] + [0] * 6}
    week = [{"date": f"2026-08-{d}", "total_flights": n} for d, n in
            (("17", 298), ("18", 341), ("19", 375), ("20", 330), ("21", 375), ("22", 321), ("23", 324))]
    sun = sun_times.sun_times(today, 38.8521, -77.0402, "America/New_York")
    fact = {"title": "THE 757 CLUB", "lines": ["ONLY BOEING NARROWBODY", "RATED FOR ETOPS 180", "DCA SEES ONE A DAY"]}
    custom = ["{red}" * 22, "{white}{white}{white}   WELCOME TO   {white}{white}{white}", "{blue}" * 22,
              "{white}{white} WASHINGTON, D.C. {white}{white}", "{blue}" * 22, "{red}" * 7 + " AILEOS! " + "{red}" * 6]
    return [
        ("CARD — today's specials checklist", idle_cards.render_specials_card(specials, today, footer)),
        ("CARD — yesterday in review", idle_cards.render_yesterday_card(yesterday, footer)),
        ("CARD — hall of fame", idle_cards.render_hall_of_fame_card(
            {"total": 28121, "king": "N33PP", "king_count": 47, "top_type": "CRJ7", "top_type_count": 4102}, footer)),
        ("CARD — last 7 days", idle_cards.render_week_card(week, footer)),
        ("CARD — runway flow", idle_cards.render_flow_card({"flow": "north", "arr": 0, "dep": 3, "sample_size": 3}, footer)),
        ("CARD — sun & golden hour", idle_cards.render_sun_card(sun, today, footer)),
        ("CARD — Claude fun fact", idle_cards.render_fact_card(fact, footer)),
        ("CARD — custom message ({red}/{white}/{blue} tokens)", idle_cards.render_custom_card(custom)),
    ]


def _print_frame(title: str, matrix: list[list[int]]) -> None:
    ruler = "0         1         2"
    sub_ruler = "0123456789012345678901"
    print()
    print("=" * 50)
    print(title)
    print("=" * 50)
    print(f"  {ruler}")
    print(f"  {sub_ruler}")
    print(" +" + "-" * 22 + "+")
    for line in render_ascii(matrix).splitlines():
        print(f" |{line}|")
    print(" +" + "-" * 22 + "+")


def main() -> None:
    for title, ac_fn, ap_fn in SCENARIOS:
        _print_frame(title, format_board(ac_fn(), ap_fn()))
    for title, matrix in _card_scenarios():
        _print_frame(title, matrix)


if __name__ == "__main__":
    main()
