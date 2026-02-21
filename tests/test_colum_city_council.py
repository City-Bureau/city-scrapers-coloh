from datetime import datetime
from os.path import dirname, join

import pytest
from city_scrapers_core.constants import CITY_COUNCIL, COMMITTEE
from city_scrapers_core.utils import file_response
from freezegun import freeze_time

from city_scrapers.spiders.colum_city_council import ColumCityCouncilSpider

# Fixtures


@pytest.fixture(scope="module")
def spider():
    return ColumCityCouncilSpider()


@pytest.fixture(scope="module")
def legistar_response():
    return file_response(
        join(dirname(__file__), "files", "colum_city_council.html"),
        url="https://columbus.legistar.com/Calendar.aspx",
    )


@pytest.fixture(scope="module")
def upcoming_response():
    return file_response(
        join(dirname(__file__), "files", "colum_city_council_upcoming.json"),
        url="https://www.columbus.gov/ocapi/calendars/getcalendaritems",
    )


@pytest.fixture(scope="module")
def legistar_items(spider, legistar_response):
    with freeze_time("2026-02-21"):
        return list(
            spider.parse_legistar(spider._parse_legistar_events(legistar_response))
        )


@pytest.fixture(scope="module")
def upcoming_items(spider, upcoming_response):
    with freeze_time("2026-02-21"):
        return list(spider.parse_upcoming(upcoming_response))


EXPECTED_LOCATION = {
    "name": "City Council Chambers, Rm 231",
    "address": "90 West Broad Street, Columbus, OH, 43215",
}


# Legistar Tests


def test_legistar_count(legistar_items):
    assert len(legistar_items) == 4


def test_legistar_first_item(legistar_items):
    item = legistar_items[0]

    assert item["title"] == "Zoning Committee"
    assert item["description"] == ""
    assert item["start"] == datetime(2026, 2, 23, 18, 30)
    assert item["end"] is None
    assert item["time_notes"] == ""
    assert item["id"] == "colum_city_council/202602231830/x/zoning_committee"
    assert item["status"] == "tentative"
    assert item["classification"] == COMMITTEE
    assert item["location"] == EXPECTED_LOCATION

    assert item["links"] == [
        {
            "href": "https://columbus.legistar.com/View.ashx?M=A&ID=1390531&GUID=99C8E536-23D1-4912-AEC7-17CB05C140AE", # noqa
            "title": "Agenda",
        },
        {
            "href": "https://columbus.legistar.com/View.ashx?M=AADA&ID=1390531&GUID=99C8E536-23D1-4912-AEC7-17CB05C140AE", # noqa
            "title": "Accessible Agenda",
        },
        {
            "href": "https://columbus.legistar.com/MeetingDetail.aspx?ID=1390531&GUID=99C8E536-23D1-4912-AEC7-17CB05C140AE&Options=info|&Search=", # noqa
            "title": "Meeting Details",
        },
    ]

    assert item["source"] == (
        "https://columbus.legistar.com/MeetingDetail.aspx"
        "?ID=1390531&GUID=99C8E536-23D1-4912-AEC7-17CB05C140AE&Options=info|&Search="
    )


# Upcoming Tests


def test_upcoming_count(upcoming_items):
    assert len(upcoming_items) == 50


def test_upcoming_first_item(upcoming_items, spider):
    item = upcoming_items[0]

    assert item["title"] == "Council Meeting"
    assert item["description"] == ""
    assert item["start"] == datetime(2026, 3, 2, 17, 0)
    assert item["end"] is None
    assert item["time_notes"] == ""
    assert item["id"] == "colum_city_council/202603021700/x/council_meeting"
    assert item["status"] == "tentative"
    assert item["classification"] == CITY_COUNCIL
    assert item["location"] == EXPECTED_LOCATION
    assert item["links"] == []
    assert item["source"] == spider.source_url


def test_no_meetings_filtered(upcoming_items):
    titles = [item["title"] for item in upcoming_items]
    assert "No Meetings" not in titles


# Shared Tests


@pytest.mark.parametrize("fixture_name", ["legistar_items", "upcoming_items"])
def test_all_day_false(request, fixture_name):
    items = request.getfixturevalue(fixture_name)
    for item in items:
        assert item["all_day"] is False
