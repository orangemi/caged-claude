"""Timezones of a country, from the bundled tzdata zone.tab.

The cage's timezone must belong to the node's exit country: either the one the
user set, or one chosen from the exit IP's location (docs/configuration.md).
"""
from pathlib import Path
import zoneinfo

DATA = Path(__file__).with_name('zone.tab')
# zone.tab lists a country's zones west to east or north to south; where its
# first zone is not where most people live, the default names that one.
DEFAULT = {'AU': 'Australia/Sydney', 'BR': 'America/Sao_Paulo', 'CA': 'America/Toronto',
           'RU': 'Europe/Moscow', 'UA': 'Europe/Kyiv', 'UZ': 'Asia/Tashkent'}
_table = None


def table():
    """{zone: country}, plus {country: [zones]} in zone.tab order."""
    global _table
    if _table is None:
        zones, countries = {}, {}
        for line in DATA.read_text().splitlines():
            if line and not line.startswith('#'):
                country, zone = line.split('\t')
                zones[zone] = country
                countries.setdefault(country, []).append(zone)
        _table = zones, countries
    return _table


def tzfile(name):
    for root in zoneinfo.TZPATH:
        try:
            return (Path(root) / name).read_bytes()
        except OSError:
            continue
    return None


def country_of(name):
    """The country of a zone; an alias such as US/Eastern counts as its zone."""
    zones, _ = table()
    if name in zones:
        return zones[name]
    data = tzfile(name) if '..' not in name else None
    if data:
        for zone, country in zones.items():
            if tzfile(zone) == data:
                return country
    return None


def for_country(country, located=None):
    """The located zone when it lies in the country, else the country's default."""
    _, countries = table()
    names = countries.get(country) or []
    if located in names:
        return located
    return DEFAULT.get(country) or (names[0] if names else None)


def examples(country, limit=3):
    _, countries = table()
    names = countries.get(country) or []
    first = for_country(country)
    return ([first] if first else []) + [name for name in names if name != first][:limit - 1]
