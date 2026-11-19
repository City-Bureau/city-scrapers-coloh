import logging
from datetime import datetime
from os.path import dirname, join
from unittest.mock import MagicMock

import pytest
from city_scrapers_core.constants import BOARD, CANCELLED, COMMITTEE, PASSED, TENTATIVE
from freezegun import freeze_time
from scrapy import Request, signals
from scrapy.http import HtmlResponse
from scrapy_playwright.page import PageMethod
from twisted.python.failure import Failure

from city_scrapers.spiders.colum_boe import ColumBoeSourceError, ColumBoeSpider

MEETING_URL = "https://go.boarddocs.com/oh/columbus/Board.nsf/goto?open&id={}"
DETAIL_URL = "https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetMeeting?open&0.123456789012345"  # noqa
AGENDA_URL = "https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetAgenda?open&0.123456789012345"  # noqa
LIST_URL = "https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetMeetingsList?open&0.123456789012345"  # noqa

# Same markup as the BD-GetMeeting page (tests/files/colum_boe.html), with
# description text copied from the live meetings named in each test
DETAIL_TEMPLATE = """
<div id="wrap-meeting"><div class="content-navigation"><div class="social">
<div class="buttons"><button class="url" title="Copy URL to Clipboard"
data-clipboard-text="https://go.boarddocs.com/oh/columbus/Board.nsf/goto?open&id={id}">
</button></div></div></div>
<div id="view-meeting" tabindex="0" aria-live="assertive" class="">
<div role="heading" aria-level="1" class="meeting-name">{title}</div>
<div class="meeting-date">{date}</div>
<div class="meeting-description">{description}</div>
</div></div>
"""

VARIANTS = {
    # The ticket's example: "ST." instead of "STREET" used to give TBD
    "jun_25": (
        "REGULAR BOARD BUSINESS MEETING (Closing the Books)",
        "Thursday, June 25, 2026",
        "THE BOARD OF EDUCATION<br>COLUMBUS EDUCATION CENTER<br>270 E. STATE ST."
        "<br>COLUMBUS, OH 43215<br>6:00 P.M.<br>DR. ANTOINETTE MIRANDA, PRESIDENT",
    ),
    # Room on its own line, city without ZIP, "EST" after the time
    "dec_8": (
        "POLICY AND GOVERNANCE COMMITTEE MEETING",
        "Monday, December 8, 2025",
        "THE BOARD OF EDUCATION<br>270 E. STATE STREET<br>ASSEMBLY ROOM"
        "<br>COLUMBUS, OHIO<br>6:00 PM EST<br>SARAH INGLES, CHAIR",
    ),
    # Street and city on one line, a time range
    "aug_28": (
        "SPECIAL BOARD MEETING (RETREAT)",
        "Friday, August 28, 2026",
        "THE BOARD OF EDUCATION<br>CENTRAL EDUCATION CENTER"
        "<br>270 E. STATE ST. COLUMBUS, OH 43215<br>9:00AM-4:00PM"
        "<br>DR. ANTOINETTE MIRANDA, PRESIDENT",
    ),
    # A range whose first time has no a.m./p.m. of its own
    "nov_13": (
        "JOINT COMMITTEE MEETING",
        "Thursday, November 13, 2025",
        "THE BOARD OF EDUCATION<br>270 E. STATE ST.<br>COLUMBUS, OH 43215"
        "<br>5:30-7:30 P.M.<br>MICHAEL COLE, PRESIDENT",
    ),
    # No time anywhere in the description
    "dec_2": (
        "REGULAR BOARD BUSINESS MEETING",
        "Tuesday, December 2, 2025",
        "COLUMBUS CITY SCHOOLS<br>BOARD OF EDUCATION MEETING<br>3700 S. HIGH ST."
        "<br>COLUMBUS, OH 43207<br>MICHAEL COLE, PRESIDENT",
    ),
    # Postponement written into the title
    "jan_27": (
        "Postponed to February 3, 2026--REGULAR BOARD BUSINESS MEETING",
        "Tuesday, January 27, 2026",
        "COLUMBUS CITY SCHOOLS<br>BOARD OF EDUCATION MEETING<br>3700 S. HIGH ST."
        "<br>COLUMBUS, OH 43207<br>DR. ANTOINETTE MIRANDA, PRESIDENT"
        "<br>Meeting has been postponed to February 3, 2026",
    ),
    # Same postponement line copied into a later, unrelated meeting
    "oct_20": (
        "REGULAR BOARD BUSINESS MEETING",
        "Tuesday, October 20, 2026",
        "COLUMBUS CITY SCHOOLS<br>BOARD OF EDUCATION MEETING<br>3700 S. HIGH ST."
        "<br>COLUMBUS, OH 43207<br>DR. ANTOINETTE MIRANDA, PRESIDENT"
        "<br>Meeting has been postponed to February 3, 2026",
    ),
    "sep_28": (
        "SPECIAL BOARD MEETING-CANCELED",
        "Monday, September 28, 2026",
        "COLUMBUS CITY SCHOOLS<br>BOARD OF EDUCATION MEETING<br>3700 S. HIGH ST."
        "<br>COLUMBUS, OH 43207<br>6:00PM<br>DR. ANTOINETTE MIRANDA, PRESIDENT",
    ),
    # A second time on the line that is not the end of a range (the
    # Sep 17, 2026 meeting adds a "( New Member Orientation" note)
    "sep_17": (
        "JOINT COMMITTEE MEETING",
        "Thursday, September 17, 2026",
        "THE BOARD OF EDUCATION<br>270 E. STATE STREET<br>COLUMBUS, OH 43215"
        "<br>5:30 PM ( New Member Orientation at 4:00 PM )",
    ),
    # The other spelling of the Southland Center address
    "jan_6": (
        "ORGANIZATIONAL MEETING",
        "Tuesday, January 6, 2026",
        "THE BOARD OF EDUCATION<br>SOUTHLAND CENTER<br>3700 SOUTH HIGH STREET"
        "<br>COLUMBUS, OHIO<br>5:00 P.M.<br>DR. ANTOINETTE MIRANDA, PRESIDENT",
    ),
    # A school outside the two district buildings
    "school": (
        "REGULAR BOARD BUSINESS MEETING",
        "Tuesday, March 2, 2027",
        "THE BOARD OF EDUCATION<br>LINDEN-MCKINLEY STEM ACADEMY"
        "<br>1320 DUXBERRY AVE.<br>COLUMBUS, OH 43211<br>6:00 PM",
    ),
    # Room on the street line, from Dec 7, 2023 and May 21, 2024
    "dec_7": (
        "NEIGHBORHOOD SCHOOL DEVELOPMENT PARTNERSHIP (NSDP)",
        "Thursday, December 7, 2023",
        "THE BOARD OF EDUCATION<br>SOUTHLAND CENTER<br>3700 S. HIGH STREET/ROOM 825"
        "<br>COLUMBUS, OHIO<br>8:00 A.M.<br>CHAIR PRO TEM",
    ),
    "may_21": (
        "REGULAR BOARD BUSINESS MEETING",
        "Tuesday, May 21, 2024",
        "THE BOARD OF EDUCATION<br>SOUTHLAND CENTER<br>3700 S. HIGH ST. RM 825"
        "<br>COLUMBUS, OH 43207<br>6:00 P.M.<br>CHRISTINA A. VERA- PRESIDENT",
    ),
    # Room line written as "IN-PERSON (ROOM 825)", from Nov 15, 2023
    "nov_15": (
        "GOVERNANCE, POLICY & ADVOCACY COMMITTEE MEETING",
        "Wednesday, November 15, 2023",
        "THE BOARD OF EDUCATION<br>SOUTHLAND CENTER<br>3700 SOUTH HIGH STREET"
        "<br>IN-PERSON (ROOM 825)<br>COLUMBUS, OHIO<br>9:00 AM EST"
        "<br>ERIC BROWN, CHAIR",
    ),
    # Misspelled "COMMITEE" and a suite on the street line of another
    # building, from Dec 13, 2023 and Feb 3, 2024
    "dec_13": (
        "FINANCE & APPROPRIATIONS COMMITEE MEETING",
        "Wednesday, December 13, 2023",
        "THE BOARD OF EDUCATION<br>SOUTHLAND CENTER<br>3700 SOUTH HIGH STREET"
        "<br>RM 825<br>COLUMBUS, OHIO<br>5:00 P.M.<br>MICHAEL COLE, CHAIR",
    ),
    "feb_3": (
        "SPECIAL BOARD MEETING",
        "Saturday, February 3, 2024",
        "THE BOARD OF EDUCATION<br>THE DENNISION EDGE<br>300 MARCONI BLVD SUITE 200"
        "<br>COLUMBUS, OH 43215<br>9:00 AM<br>CHRISTINA A. VERA - PRESIDENT",
    ),
    # The Feb 17, 2026 description with inline formatting added, which must
    # not split "6:00 PM" or the street across lines
    "inline": (
        "REGULAR BOARD BUSINESS MEETING",
        "Tuesday, February 17, 2026",
        "<p>COLUMBUS CITY SCHOOLS<br>BOARD OF EDUCATION MEETING</p>"
        "<p><span>3700 S. HIGH</span> ST. COLUMBUS, OH 43207</p>"
        "<p><strong>6:00</strong> <em>PM</em> &amp; online</p>",
    ),
    # A newline inside the HTML source is a space, not a new line
    "source_newline": (
        "REGULAR BOARD BUSINESS MEETING",
        "Tuesday, February 17, 2026",
        "COLUMBUS CITY SCHOOLS<br>3700 S. HIGH ST.\nCOLUMBUS, OH 43207"
        "<p>6:00\nPM</p>",
    ),
    "no_address": (
        "SPECIAL BOARD MEETING",
        "Tuesday, March 9, 2027",
        "THE BOARD OF EDUCATION<br>6:00 PM",
    ),
}


def detail_response(key, meeting_id="ID123"):
    title, date, description = VARIANTS[key]
    body = DETAIL_TEMPLATE.format(
        id=meeting_id, title=title, date=date, description=description
    )
    return HtmlResponse(url=DETAIL_URL, body=body.encode(), encoding="utf-8")


@pytest.fixture(scope="module")
def spider():
    return ColumBoeSpider()


def read_file(name):
    with open(join(dirname(__file__), "files", name), encoding="utf-8") as f:
        return f.read()


def fetched(text, url=DETAIL_URL, status=200):
    """One call's result as fetch_all_js returns it."""
    return {"url": url, "status": status, "text": text}


def results_response(results):
    """The public page's response, carrying fetch_all_js's results the way
    scrapy-playwright stores a PageMethod's return value."""
    page_method = PageMethod("evaluate", "() => {}")
    page_method.result = results
    request = Request(
        ColumBoeSpider.source_url,
        meta={"playwright_page_methods": {"fetch_all": page_method}},
    )
    return HtmlResponse(
        url=ColumBoeSpider.source_url, body=b"", encoding="utf-8", request=request
    )


def run_results(spider, results, now="2026-02-24"):
    with freeze_time(now):
        return list(spider._parse_results(results_response(results)))


LIST_RESULT = fetched(read_file("colum_boe_meetings.json"), url=LIST_URL)
# BD-GetAgenda and BD-GetMeeting for DQ3VPX80F260, the first meeting kept
DQ3_RESULT = {
    "agenda": fetched(read_file("colum_boe_agenda.html"), url=AGENDA_URL),
    "meeting": fetched(read_file("colum_boe.html")),
}


@pytest.fixture(scope="module")
def item():
    spider = ColumBoeSpider()
    items = run_results(
        spider, {"list": LIST_RESULT, "meetings": {"DQ3VPX80F260": DQ3_RESULT}}
    )
    return items[0]


@pytest.fixture(scope="module")
def variants(spider):
    with freeze_time("2026-11-12"):
        return {key: next(spider.parse(detail_response(key))) for key in VARIANTS}


def test_start_request_runs_fetch_all_in_public_page(spider):
    with freeze_time("2026-02-24"):
        requests = list(spider.start_requests())
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://go.boarddocs.com/oh/columbus/Board.nsf/Public"
    assert request.callback == spider._parse_results
    assert request.errback == spider._handle_error
    assert request.meta["playwright"] is True
    page_method = request.meta["playwright_page_methods"]["fetch_all"]
    assert page_method.method == "evaluate"
    script, args = page_method.args
    assert script == spider.fetch_all_js
    # The frozen 2026-02-24 00:00 UTC is still Feb 23 in Columbus
    assert args == {
        "baseUrl": spider.base_url,
        "committeeId": "A9HCVU32F33A",
        "cutoff": "20230223",
        "delayMs": 1000,
        "timeoutMs": 30000,
        "maxFailures": 5,
    }


def test_list_keeps_three_years(caplog):
    """Every meeting since the cutoff (2023-02-23, whose NSDP meeting is kept)
    is expected in the results; here none were read, so each is counted."""
    spider = ColumBoeSpider()
    with caplog.at_level(logging.WARNING):
        assert run_results(spider, {"list": LIST_RESULT, "meetings": {}}) == []
    assert spider._failed_requests == 185
    assert "185 BoardDocs meetings were not read: DQ3VPX80F260, " in caplog.text


def test_stopped_run_parses_what_was_read(caplog):
    """After fetch_all_js stops, the meetings it read are still parsed and
    the rest are counted as not read."""
    spider = ColumBoeSpider()
    results = {
        "list": LIST_RESULT,
        "meetings": {"DQ3VPX80F260": DQ3_RESULT},
        "stopped": "5 failed calls in a row",
    }
    with caplog.at_level(logging.WARNING):
        items = run_results(spider, results)
    assert len(items) == 1
    assert spider._failed_requests == 184
    assert "Stopped reading BoardDocs after 5 failed calls in a row" in caplog.text
    assert "184 BoardDocs meetings were not read" in caplog.text


def test_cutoff_is_set_once_per_run():
    """A run that starts on Feb 23 in Columbus and is parsed on Feb 24 still
    expects the Feb 23, 2023 meeting that fetch_all_js was told to read."""
    spider = ColumBoeSpider()
    with freeze_time("2026-02-24"):
        list(spider.start_requests())
    run_results(spider, {"list": LIST_RESULT, "meetings": {}}, now="2026-02-25")
    assert spider._failed_requests == 185


def test_agenda_link_is_first_agenda_item(item):
    # The first agenda item, as the original spider linked it
    assert item["links"][0]["href"] == MEETING_URL.format("DQ3VPZ80F262")


def test_meeting_without_agenda_items(spider):
    response = HtmlResponse(url=AGENDA_URL, body=b"<div></div>", encoding="utf-8")
    assert spider._parse_agenda_id(response, "ABC") is None


def test_failed_agenda_still_parses_meeting():
    spider = ColumBoeSpider()
    meeting = {**DQ3_RESULT, "agenda": fetched(" ", url=AGENDA_URL)}
    items = run_results(
        spider, {"list": LIST_RESULT, "meetings": {"DQ3VPX80F260": meeting}}
    )
    assert len(items) == 1
    assert items[0]["links"] == []
    # The blank agenda and the 184 meetings not in the results
    assert spider._failed_requests == 185


def test_failed_meeting_is_skipped():
    spider = ColumBoeSpider()
    meeting = {**DQ3_RESULT, "meeting": fetched("", status=0)}
    items = run_results(
        spider, {"list": LIST_RESULT, "meetings": {"DQ3VPX80F260": meeting}}
    )
    assert items == []
    assert spider._failed_requests == 185


def test_blocked_list_is_counted_and_logged(caplog):
    """CloudFront's block is a 200 with a blank body."""
    spider = ColumBoeSpider()
    with caplog.at_level(logging.WARNING):
        items = run_results(
            spider, {"list": fetched(" ", url=LIST_URL), "meetings": {}}
        )
    assert items == []
    assert spider._failed_requests == 1
    assert "BD-GetMeetingsList returned 200 with 1 bytes" in caplog.text


def test_list_that_is_not_json(caplog):
    spider = ColumBoeSpider()
    with caplog.at_level(logging.WARNING):
        items = run_results(
            spider, {"list": fetched("<html></html>", url=LIST_URL), "meetings": {}}
        )
    assert items == []
    assert "BoardDocs meetings list is not JSON" in caplog.text


def reported_errors(spider):
    """The exceptions the spider sent through Scrapy's spider_error signal,
    which scrapy_sentry_errors forwards to Sentry in production."""
    return [
        c.kwargs["failure"].value
        for c in spider.crawler.signals.send_catch_log.call_args_list
        if c.kwargs.get("signal") is signals.spider_error
    ]


def closed_spider(failed_requests=0, meetings_parsed=0, reason="finished"):
    """A spider with a mock crawler, closed after a run with these counts."""
    spider = ColumBoeSpider()
    spider.crawler = MagicMock()
    spider._failed_requests = failed_requests
    spider._meetings_parsed = meetings_parsed
    spider._on_spider_closed(spider, reason)
    return spider


def test_failed_request_is_counted_and_logged(caplog):
    """caplog captures log records, so this checks that a failed BoardDocs
    request (for example a 403 from CloudFront) is logged and counted for
    the error reported when the spider closes."""
    spider = ColumBoeSpider()
    with caplog.at_level(logging.WARNING):
        spider._handle_error(Failure(Exception("403 Forbidden")))
    assert spider._failed_requests == 1
    assert "BoardDocs request failed" in caplog.text


def test_blocked_run_reports_one_error():
    spider = closed_spider(failed_requests=2)
    errors = reported_errors(spider)
    assert len(errors) == 1
    assert isinstance(errors[0], ColumBoeSourceError)
    assert str(errors[0]) == (
        "colum_boe: 2 failed request(s); 0 meetings parsed "
        "(finish_reason=finished, meetings=0)"
    )


def test_some_failed_meetings_are_reported():
    errors = reported_errors(closed_spider(failed_requests=1, meetings_parsed=171))
    assert [str(e) for e in errors] == [
        "colum_boe: 1 failed request(s) (finish_reason=finished, meetings=171)"
    ]


def test_good_run_reports_nothing():
    assert reported_errors(closed_spider(meetings_parsed=172)) == []


def test_parsed_meetings_are_counted():
    spider = ColumBoeSpider()
    with freeze_time("2026-11-12"):
        list(spider.parse(detail_response("jun_25")))
        list(spider.parse(detail_response("no_address")))
    assert spider._meetings_parsed == 2


def test_title(item):
    assert item["title"] == "REGULAR BOARD BUSINESS MEETING"


def test_description(item):
    assert item["description"] == (
        "COLUMBUS CITY SCHOOLS BOARD OF EDUCATION MEETING 3700 S. HIGH ST. "
        "COLUMBUS, OH 43207 6:00PM DR. ANTOINETTE MIRANDA, PRESIDENT"
    )


def test_classification(item):
    assert item["classification"] == BOARD


def test_start(item):
    assert item["start"] == datetime(2026, 2, 17, 18, 0)


def test_end(item):
    assert item["end"] is None


def test_time_notes(item):
    assert item["time_notes"] == ""


def test_id(item):
    assert item["id"] == "colum_boe/202602171800/x/regular_board_business_meeting"


def test_status(item):
    assert item["status"] == PASSED


def test_location(item):
    # The description gives only "COLUMBUS CITY SCHOOLS" and the address
    assert item["location"] == {
        "name": "Columbus City Schools",
        "address": "3700 S. High Street, Columbus, OH 43207",
    }


def test_source(item):
    assert item["source"] == MEETING_URL.format("DQ3VPX80F260")


def test_links(item):
    assert item["links"] == [
        {"title": "Agenda", "href": MEETING_URL.format("DQ3VPZ80F262")}
    ]


def test_all_day(item):
    assert item["all_day"] is False


def test_abbreviated_street_location(variants):
    assert variants["jun_25"]["location"] == {
        "name": "Columbus Education Center",
        "address": "270 E. State Street, Columbus, OH 43215",
    }
    assert variants["jun_25"]["start"] == datetime(2026, 6, 25, 18, 0)


def test_room_goes_in_name(variants):
    meeting = variants["dec_8"]
    assert meeting["location"] == {
        "name": "Columbus Education Center, Assembly Room",
        "address": "270 E. State Street, Columbus, OH 43215",
    }
    assert meeting["start"] == datetime(2025, 12, 8, 18, 0)
    assert meeting["classification"] == COMMITTEE


def test_one_line_address_and_time_range(variants):
    meeting = variants["aug_28"]
    assert meeting["location"] == {
        "name": "Central Education Center",
        "address": "270 E. State Street, Columbus, OH 43215",
    }
    assert meeting["start"] == datetime(2026, 8, 28, 9, 0)
    assert meeting["end"] == datetime(2026, 8, 28, 16, 0)


def test_range_shares_meridiem(variants):
    assert variants["nov_13"]["start"] == datetime(2025, 11, 13, 17, 30)
    assert variants["nov_13"]["end"] == datetime(2025, 11, 13, 19, 30)


def test_second_time_is_not_an_end(variants):
    assert variants["sep_17"]["start"] == datetime(2026, 9, 17, 17, 30)
    assert variants["sep_17"]["end"] is None


def test_spelled_out_southland_address(variants):
    assert variants["jan_6"]["location"] == {
        "name": "Southland Center",
        "address": "3700 S. High Street, Columbus, OH 43207",
    }
    assert variants["jan_6"]["start"] == datetime(2026, 1, 6, 17, 0)


def test_missing_time(spider, caplog):
    """caplog collects the warning the spider logs when a description has no
    time, so a meeting stored at 00:00 is never silent."""
    with caplog.at_level(logging.WARNING), freeze_time("2026-11-12"):
        meeting = next(spider.parse(detail_response("dec_2")))
    assert meeting["start"] == datetime(2025, 12, 2, 0, 0)
    assert meeting["time_notes"] == (
        "Please check the meeting attachment for start time details."
    )
    assert "No start time found" in caplog.text


def test_postponed_title(variants):
    meeting = variants["jan_27"]
    assert meeting["title"] == "REGULAR BOARD BUSINESS MEETING"
    assert meeting["status"] == CANCELLED


def test_stale_postponed_description_is_not_cancelled(spider):
    with freeze_time("2026-10-01"):
        meeting = next(spider.parse(detail_response("oct_20")))
    assert meeting["status"] == TENTATIVE


def test_canceled_title(variants):
    assert variants["sep_28"]["title"] == "SPECIAL BOARD MEETING"
    assert variants["sep_28"]["status"] == CANCELLED


def test_other_building(variants):
    assert variants["school"]["location"] == {
        "name": "Linden-Mckinley Stem Academy",
        "address": "1320 Duxberry Ave., Columbus, OH 43211",
    }


def test_room_on_street_line(variants):
    for key in ["dec_7", "may_21"]:
        assert variants[key]["location"] == {
            "name": "Southland Center, Room 825",
            "address": "3700 S. High Street, Columbus, OH 43207",
        }


def test_in_person_room_line(variants):
    assert variants["nov_15"]["location"]["name"] == "Southland Center, Room 825"


def test_misspelled_committee(variants):
    assert variants["dec_13"]["classification"] == COMMITTEE
    assert variants["dec_13"]["location"]["name"] == "Southland Center, Room 825"


def test_suite_goes_in_name(variants):
    assert variants["feb_3"]["location"] == {
        "name": "The Dennision Edge, Suite 200",
        "address": "300 Marconi Blvd, Columbus, OH 43215",
    }


def test_inline_formatting_keeps_lines_whole(variants):
    meeting = variants["inline"]
    assert meeting["start"] == datetime(2026, 2, 17, 18, 0)
    assert meeting["location"] == {
        "name": "Columbus City Schools",
        "address": "3700 S. High Street, Columbus, OH 43207",
    }
    assert meeting["description"] == (
        "COLUMBUS CITY SCHOOLS BOARD OF EDUCATION MEETING "
        "3700 S. HIGH ST. COLUMBUS, OH 43207 6:00 PM & online"
    )


def test_source_newline_is_a_space(variants):
    meeting = variants["source_newline"]
    assert meeting["start"] == datetime(2026, 2, 17, 18, 0)
    assert meeting["time_notes"] == ""
    assert meeting["description"] == (
        "COLUMBUS CITY SCHOOLS 3700 S. HIGH ST. COLUMBUS, OH 43207 6:00 PM"
    )


def test_no_address(variants):
    assert variants["no_address"]["location"] == {"name": "TBD", "address": ""}


def test_page_url_falls_back_to_meeting_id(spider):
    response = HtmlResponse(
        url=DETAIL_URL,
        body=detail_response("jun_25").body.replace(b'class="url"', b'class="x"'),
        encoding="utf-8",
    )
    with freeze_time("2026-11-12"):
        meeting = next(spider.parse(response, meeting_id="ABC"))
    assert meeting["source"] == MEETING_URL.format("ABC")
    # No agenda id was passed, so there is no agenda link
    assert meeting["links"] == []


def test_no_access_page_is_skipped(spider):
    response = HtmlResponse(
        url=DETAIL_URL, body=b"<html><body>No access</body></html>", encoding="utf-8"
    )
    assert list(spider.parse(response, meeting_id="ABC")) == []
