from datetime import datetime
from os.path import dirname, join

import pytest
from city_scrapers_core.constants import BOARD, PASSED
from city_scrapers_core.utils import file_response
from freezegun import freeze_time

from city_scrapers.spiders.colum_boe import ColumBoeSpider


@pytest.fixture
def spider():
    return ColumBoeSpider()


@pytest.fixture
def meetings_response(spider):
    response = file_response(
        join(dirname(__file__), "files", "colum_boe_meetings.json"),
        url="https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetMeetingsList?open&0.123456789012345",  # noqa
    )
    with freeze_time("2026-02-24"):
        return list(spider._get_meeting_detail(response))


@pytest.fixture
def parsed_items(spider):
    detail_response = file_response(
        join(dirname(__file__), "files", "colum_boe_detail.html"),
        url="https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetMeeting?open&0.123456789012345",  # noqa
    )
    agenda_response = file_response(
        join(dirname(__file__), "files", "colum_boe_agenda.html"),
        url="https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetAgenda?open&0.123456789012345",  # noqa
    )
    agenda_response.meta["detail_response"] = detail_response
    agenda_response.meta["raw_description"] = detail_response.css(
        ".meeting-description::text"
    ).getall()

    with freeze_time("2026-02-24"):
        return [item for item in spider.parse(agenda_response)]


def test_count(meetings_response):
    assert len(meetings_response) == 184


def test_title(parsed_items):
    assert parsed_items[0]["title"] == "REGULAR BOARD BUSINESS MEETING"


def test_description(parsed_items):
    assert isinstance(parsed_items[0]["description"], str)
    assert len(parsed_items[0]["description"]) > 0


def test_start(parsed_items):
    assert parsed_items[0]["start"] == datetime(2026, 2, 17, 18, 0)


def test_end(parsed_items):
    assert parsed_items[0]["end"] is None


def test_time_notes(parsed_items):
    assert parsed_items[0]["time_notes"] == ""


def test_id(parsed_items):
    assert (
        parsed_items[0]["id"]
        == "colum_boe/202602171800/x/regular_board_business_meeting"
    )


def test_status(parsed_items):
    assert parsed_items[0]["status"] == PASSED


def test_location(parsed_items):
    assert parsed_items[0]["location"] == {
        "name": "COLUMBUS CITY SCHOOLS",
        "address": "3700 S. HIGH ST. COLUMBUS, OH 43207",
    }


def test_source(parsed_items):
    assert (
        parsed_items[0]["source"]
        == "https://go.boarddocs.com/oh/columbus/Board.nsf/Public"
    )


def test_links(parsed_items):
    assert parsed_items[0]["links"] == [
        {
            "href": "https://go.boarddocs.com/oh/columbus/Board.nsf/goto?open&id=DQ3VPZ80F262",  # noqa
            "title": "Agenda",
        }
    ]


def test_classification(parsed_items):
    assert parsed_items[0]["classification"] == BOARD


def test_all_day(parsed_items):
    for item in parsed_items:
        assert item["all_day"] is False
