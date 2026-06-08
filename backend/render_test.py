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


def main() -> None:
    ruler = "0         1         2"
    sub_ruler = "0123456789012345678901"
    for title, ac_fn, ap_fn in SCENARIOS:
        print()
        print("=" * 50)
        print(title)
        print("=" * 50)
        print(f"  {ruler}")
        print(f"  {sub_ruler}")
        print(" +" + "-" * 22 + "+")
        matrix = format_board(ac_fn(), ap_fn())
        ascii_render = render_ascii(matrix)
        for line in ascii_render.splitlines():
            print(f" |{line}|")
        print(" +" + "-" * 22 + "+")


if __name__ == "__main__":
    main()
