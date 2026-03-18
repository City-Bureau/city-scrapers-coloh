from datetime import datetime
from os.path import dirname, join

import pytest
from city_scrapers_core.constants import COMMISSION
from city_scrapers_core.utils import file_response
from freezegun import freeze_time
from scrapy.http import Request, TextResponse

from city_scrapers.spiders.colum_franklin_boc import ColumFranklinBocSpider

# --- Calendar API response (parse method) ---

calendar_response = file_response(
    join(dirname(__file__), "files", "colum_franklin_boc.json"),
    url="https://www.franklincountyohio.gov/ocapi/calendars/getcalendaritems",
)

spider = ColumFranklinBocSpider()

freezer = freeze_time("2025-12-01")
freezer.start()

# parse() yields Request objects for each valid meeting
calendar_requests = list(spider.parse(calendar_response))

freezer.stop()


# --- Detail response (parse_detail method) ---


def _make_detail_response(fixture_name, url, meta):
    """Helper to create a detail response with meta from a fixture file."""
    fixture_path = join(dirname(__file__), "files", fixture_name)
    with open(fixture_path, "rb") as f:
        body = f.read()
    request = Request(url=url, meta=meta)
    return TextResponse(url=url, body=body, request=request)


# General Session detail
general_session_response = _make_detail_response(
    "colum_franklin_boc_detail.json",
    url=(
        "https://www.franklincountyohio.gov/OCServiceHandler.axd"
        "?url=ocsvc/Public/meetings/documentrenderer"
        "&cvid=ef1d11f9-3abd-4782-ae11-a366eb220212"
    ),
    meta={"title": "General Session", "datetime_str": "5/6/2025 9:00:00 AM"},
)

# Briefing Session detail (different location format)
briefing_session_response = _make_detail_response(
    "colum_franklin_boc_detail_briefing.json",
    url=(
        "https://www.franklincountyohio.gov/OCServiceHandler.axd"
        "?url=ocsvc/Public/meetings/documentrenderer"
        "&cvid=e132bd53-68c8-467e-a5db-9869a5715d1d"
    ),
    meta={
        "title": "Commissioners' Briefing Session",
        "datetime_str": "5/15/2025 9:00:00 AM",
    },
)

# Planning Commission detail (second calendar)
planning_commission_response = _make_detail_response(
    "colum_franklin_boc_detail_planning.json",
    url=(
        "https://www.franklincountyohio.gov/OCServiceHandler.axd"
        "?url=ocsvc/Public/meetings/documentrenderer"
        "&cvid=3fac85ae-e3f2-4899-a351-0b936b567b6c"
    ),
    meta={
        "title": "Planning Commission Meeting",
        "datetime_str": "5/14/2025 1:30:00 PM",
    },
)

# Invalid detail (non-meeting content type)
invalid_response = _make_detail_response(
    "colum_franklin_boc_detail_invalid.json",
    url=(
        "https://www.franklincountyohio.gov/OCServiceHandler.axd"
        "?url=ocsvc/Public/meetings/documentrenderer"
        "&cvid=d01b5701-0475-4b56-ac3b-f98f5d2b7ffa"
    ),
    meta={
        "title": "Uplift Her Wellness Day 2025",
        "datetime_str": "6/18/2025 2:00:00 PM",
    },
)

freezer = freeze_time("2025-12-01")
freezer.start()

general_items = list(spider.parse_detail(general_session_response))
briefing_items = list(spider.parse_detail(briefing_session_response))
planning_items = list(spider.parse_detail(planning_commission_response))
invalid_items = list(spider.parse_detail(invalid_response))

freezer.stop()

parsed_item = general_items[0]
briefing_item = briefing_items[0]
planning_item = planning_items[0]


# === Calendar filtering tests ===


def test_calendar_request_count():
    """Fixture has 8 items total; all are yielded as detail requests."""
    assert len(calendar_requests) == 8


def test_calendar_request_urls():
    """Each request should target the detail endpoint."""
    for req in calendar_requests:
        assert "OCServiceHandler.axd" in req.url
        assert "documentrenderer" in req.url


def test_calendar_request_meta():
    """Each request should carry title and datetime_str in meta."""
    for req in calendar_requests:
        assert "title" in req.meta
        assert "datetime_str" in req.meta


# === Invalid detail response test ===


def test_invalid_detail_yields_nothing():
    """Non-meeting content types should yield no items."""
    assert len(invalid_items) == 0


# === General Session detail tests ===


def test_count():
    assert len(general_items) == 1


def test_title():
    assert parsed_item["title"] == "General Session"


def test_description():
    assert "open to the public" in parsed_item["description"]
    assert "Board of Commissioners" in parsed_item["description"]


def test_classification():
    assert parsed_item["classification"] == COMMISSION


def test_start():
    assert parsed_item["start"] == datetime(2025, 5, 6, 9, 0)


def test_end():
    assert parsed_item["end"] is None


def test_time_notes():
    assert parsed_item["time_notes"] == ""


def test_id():
    assert parsed_item["id"] == "colum_franklin_boc/202505060900/x/general_session"


def test_status():
    assert parsed_item["status"] == "passed"


def test_location():
    assert parsed_item["location"] == {
        "name": "Commissioners' Hearing Room, Michael J. Dorrian Building, 1st Floor",
        "address": "369 South High Street, Columbus, OH, 43215",
    }


def test_links():
    assert len(parsed_item["links"]) == 1
    assert parsed_item["links"][0]["title"] == "Agenda"
    assert parsed_item["links"][0]["href"] == (
        "https://www.franklincountyohio.gov/files/assets/public/v/2/"
        "boc/documents/general-session-agendas/2025/"
        "general-session-agenda-05-06-2025.pdf"
    )


def test_source():
    assert parsed_item["source"] == (
        "https://www.franklincountyohio.gov/County-Government/"
        "About-Our-Community/Public-Meetings-and-Agendas"
    )


def test_all_day():
    assert parsed_item["all_day"] is False


# === Briefing Session detail tests (different location format) ===


def test_briefing_title():
    assert briefing_item["title"] == "Commissioners' Briefing Session"


def test_briefing_start():
    assert briefing_item["start"] == datetime(2025, 5, 15, 9, 0)


def test_briefing_location():
    """Briefing session: long instruction paragraph is excluded from name;
    last paragraph starts with street number so name stays empty."""
    assert briefing_item["location"]["name"] == ""
    assert "373 S. High St" in briefing_item["location"]["address"]
    assert "Columbus, OH" in briefing_item["location"]["address"]


def test_briefing_links():
    assert len(briefing_item["links"]) == 1
    assert briefing_item["links"][0]["title"] == "Briefing Session Agenda"
    assert "briefing-session-agenda-05-15-2025.pdf" in briefing_item["links"][0]["href"]


def test_briefing_classification():
    assert briefing_item["classification"] == COMMISSION


# === Planning Commission detail tests (second calendar) ===


def test_planning_title():
    assert planning_item["title"] == "Planning Commission Meeting"


def test_planning_start():
    assert planning_item["start"] == datetime(2025, 5, 14, 13, 30)


def test_planning_classification():
    assert planning_item["classification"] == COMMISSION


def test_planning_location():
    assert "369 S High St" in planning_item["location"]["address"]
    assert "Columbus, OH" in planning_item["location"]["address"]


def test_planning_links():
    assert len(planning_item["links"]) == 1
    assert "Planning Commission" in planning_item["links"][0]["title"]
    assert planning_item["links"][0]["href"].endswith(".pdf")


def test_planning_id():
    assert planning_item["id"] == (
        "colum_franklin_boc/202505141330/x/planning_commission_meeting"
    )


# === Parametrized tests across all parsed items ===


all_items = general_items + briefing_items + planning_items


@pytest.mark.parametrize("item", all_items)
def test_all_items_have_required_fields(item):
    assert item["title"]
    assert isinstance(item["start"], datetime)
    assert item["classification"] == COMMISSION
    assert item["all_day"] is False
    assert isinstance(item["links"], list)
    assert item["id"]
    assert item["status"] in ["tentative", "confirmed", "cancelled", "passed"]
