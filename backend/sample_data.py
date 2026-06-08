from .airline_colors import GREEN, YELLOW, RED, color_for
from .formatter import AircraftView, AirportView


def normal_united():
    return AircraftView(
        airline_iata="UA",
        flight_number=1234,
        airline_color=color_for("UA"),
        origin_iata="IAD",
        destination_iata="DCA",
        scheduled_departure="842P",
        actual_departure="848P",
        departure_delay_min=6,
        scheduled_arrival="945P",
        estimated_arrival="950P",
        arrival_delay_min=5,
        tail_number="N12345",
        year_built=2018,
        sighting_count=7,
        aircraft_name="BOEING 737-800",
    )


def rare_757():
    return AircraftView(
        airline_iata="UA",
        flight_number=1518,
        airline_color=color_for("UA"),
        origin_iata="DEN",
        destination_iata="DCA",
        scheduled_departure="725A",
        actual_departure="735A",
        departure_delay_min=10,
        scheduled_arrival="100P",
        estimated_arrival="105P",
        arrival_delay_min=5,
        tail_number="N57864",
        year_built=2000,
        sighting_count=3,
        aircraft_name="BOEING 757-200",
        is_rare=True,
        rare_reason="BOEING 757-200",
    )


def livery_aa_astrojet():
    return AircraftView(
        airline_iata="AA",
        flight_number=300,
        airline_color=color_for("AA"),
        origin_iata="JFK",
        destination_iata="DCA",
        scheduled_departure="630A",
        actual_departure="635A",
        departure_delay_min=5,
        scheduled_arrival="755A",
        estimated_arrival="800A",
        arrival_delay_min=5,
        tail_number="N905NN",
        year_built=2014,
        sighting_count=12,
        aircraft_name="BOEING 737-800",
        livery_name="AA ASTROJET",
    )


def delayed_southwest():
    return AircraftView(
        airline_iata="WN",
        flight_number=42,
        airline_color=color_for("WN"),
        origin_iata="BWI",
        destination_iata="DCA",
        scheduled_departure="610P",
        actual_departure="645P",
        departure_delay_min=35,
        scheduled_arrival="635P",
        estimated_arrival="710P",
        arrival_delay_min=35,
        tail_number="N7811F",
        year_built=2012,
        sighting_count=22,
        aircraft_name="BOEING 737-700",
    )


def regional_widebody_rare():
    return AircraftView(
        airline_iata="LH",
        flight_number=414,
        airline_color=color_for("LH"),
        origin_iata="FRA",
        destination_iata="DCA",
        scheduled_departure="430P",
        actual_departure="445P",
        departure_delay_min=15,
        scheduled_arrival="715P",
        estimated_arrival="730P",
        arrival_delay_min=15,
        tail_number="DAIHE",
        year_built=2008,
        sighting_count=1,
        aircraft_name="AIRBUS A330-300",
        is_rare=True,
        rare_reason="AIRBUS A330-300",
    )


def airport_ontime():
    return AirportView(iata="DCA", status_color=GREEN, arrivals_today=214, departures_today=198)


def airport_minor_delays():
    return AirportView(iata="DCA", status_color=YELLOW, arrivals_today=187, departures_today=165)


def airport_major_delays():
    return AirportView(iata="DCA", status_color=RED, arrivals_today=98, departures_today=72)
