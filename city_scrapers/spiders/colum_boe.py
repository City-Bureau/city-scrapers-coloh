import random
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

import scrapy
from city_scrapers_core.constants import BOARD, CANCELLED, COMMITTEE, PASSED, TENTATIVE
from city_scrapers_core.items import Meeting
from city_scrapers_core.spiders import CityScrapersSpider
from dateutil.relativedelta import relativedelta
from scrapy import signals
from twisted.python.failure import Failure
from w3lib.html import remove_tags, replace_entities


class ColumBoeSourceError(Exception):
    """BoardDocs could not be read, so meetings would be missed."""


class ColumBoeSpider(CityScrapersSpider):
    name = "colum_boe"
    agency = "Columbus Board of Education"
    timezone = "America/New_York"
    tzinfo = ZoneInfo("America/New_York")

    base_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/{endpoint}?open&0.{cache_buster}"  # noqa
    source_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/Public"
    meeting_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/goto?open&id={meeting_id}"  # noqa
    boarddocs_committee_id = "A9HCVU32F33A"
    lookback = relativedelta(years=3)

    # BoardDocs sits behind CloudFront, which now answers bare API calls with
    # 403. The requests follow City-Bureau/city-scrapers-grand-rapids#47: load
    # the public page first for a session cookie, then POST the way the page's
    # own scripts do. The browser User-Agent is required: without it
    # CloudFront still returns 403 (checked against the live site in Nov 2026).
    boarddocs_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",  # noqa
        "X-Requested-With": "XMLHttpRequest",
    }
    custom_settings = {
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": True,
        "FEED_EXPORT_ENCODING": "utf-8",
    }

    # The board meets in two district buildings. The source writes their
    # addresses several ways ("270 E. STATE ST.", "270 E. STATE STREET",
    # "COLUMBUS, OHIO" with no ZIP), so they are matched on number and street
    # and given one complete address. The names come from the descriptions
    # ("SOUTHLAND CENTER", "COLUMBUS EDUCATION CENTER") and are only used when
    # a description names no building itself.
    known_locations = {
        "270 E STATE": {
            "name": "Columbus Education Center",
            "address": "270 E. State Street, Columbus, OH 43215",
        },
        "3700 S HIGH": {
            "name": "Columbus City Schools",
            "address": "3700 S. High Street, Columbus, OH 43207",
        },
    }

    street_re = re.compile(r"^\d+\s+[A-Za-z]")
    city_re = re.compile(r"[A-Za-z .]+,\s*(?:OH|OHIO)\b\.?(?:\s*\d{5})?", re.I)
    zip_re = re.compile(r"\b\d{5}\b")
    # A start time, optionally followed by "-" and an end time:
    # "6:00PM", "6:00 P.M.", "5:30 PM-8:00 PM", "5:30-7:30 P.M."
    time_re = re.compile(
        r"(\d{1,2}):(\d{2})\s*(?:([AP])\.?\s*M\b\.?)?"
        r"(?:\s*[-–]\s*(\d{1,2}):(\d{2})\s*(?:([AP])\.?\s*M\b\.?)?)?",
        re.I,
    )
    meridiem_re = re.compile(r"\d\s*[AP]\.?\s*M\b", re.I)
    # A room or suite written on the street line: "3700 S. HIGH STREET/ROOM
    # 825", "3700 S. HIGH ST. RM 825", "300 MARCONI BLVD SUITE 200"
    street_room_re = re.compile(r"\s*/?\s*\b(?:ROOM|RM|SUITE|STE)\b\.?\s*\S+", re.I)
    # Tags that start a new description line
    line_break_re = re.compile(
        r"<br\s*/?>|</?(?:p|div|li|ul|ol|h[1-6]|tr|table)\b[^>]*>", re.I
    )
    # "COMMITTEE" and the source's misspelling "COMMITEE"
    committee_re = re.compile(r"\bcommit+ee\b", re.I)
    # Lines that name the body or the district rather than a place
    generic_name_re = re.compile(
        r"^(?:THE\s+)?BOARD OF EDUCATION(?:\s+MEETING)?$|^COLUMBUS CITY SCHOOLS$",
        re.I,
    )
    # Status text BoardDocs staff add to titles, e.g. "SPECIAL BOARD
    # MEETING-CANCELED" or "Postponed to February 3, 2026--REGULAR BOARD..."
    title_status_re = re.compile(
        r"^\s*postponed to [A-Za-z]+\s+\d{1,2},?\s+\d{4}\s*-+\s*"
        r"|\s*-+\s*(?:cancell?ed|postponed)\s*$",
        re.I,
    )

    def start_requests(self):
        yield scrapy.Request(
            self.source_url,
            headers={
                **self.boarddocs_headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",  # noqa
            },
            callback=self._request_meetings_list,
            errback=self._handle_public_page_error,
            dont_filter=True,
        )

    def _boarddocs_post(self, endpoint, body, callback, cb_kwargs=None):
        """POST to a BoardDocs endpoint the way the public page's scripts do.
        The random number in the URL is the page's own cache-buster."""
        return scrapy.Request(
            self.base_url.format(
                endpoint=endpoint,
                cache_buster=random.randint(10**14, 10**15 - 1),
            ),
            method="POST",
            body=body,
            headers={
                **self.boarddocs_headers,
                "Origin": "https://go.boarddocs.com",
                "Referer": self.source_url,
            },
            callback=callback,
            cb_kwargs=cb_kwargs or {},
            errback=self._handle_error,
            dont_filter=True,
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._failed_requests = 0
        self._meetings_parsed = 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._on_spider_closed, signal=signals.spider_closed)
        return spider

    def _handle_public_page_error(self, failure):
        """The public page only provides the session cookie, so a failure
        there is logged and the meetings list is still requested. If the
        block also stops the list, that failure is the one reported."""
        self.logger.warning(
            "BoardDocs public page request failed, continuing: %r", failure
        )
        yield from self._request_meetings_list(None)

    def _handle_error(self, failure):
        """A failed list or meeting request means missing meetings. It is
        counted and reported once when the spider closes."""
        self._failed_requests += 1
        self.logger.warning("BoardDocs request failed: %r", failure.value)

    def _on_spider_closed(self, spider, reason):
        """Report one error per run if requests failed or no meetings were
        found, so a blocked run doesn't finish as a quiet success with 0
        meetings. Follows City-Bureau/city-scrapers-colgo#39."""
        problems = []
        if self._failed_requests:
            problems.append(f"{self._failed_requests} failed request(s)")
        if reason == "finished" and self._meetings_parsed == 0:
            problems.append("0 meetings parsed")
        if not problems:
            return
        self._report_error(
            ColumBoeSourceError(
                f"{self.name}: {'; '.join(problems)} "
                f"(finish_reason={reason}, meetings={self._meetings_parsed})"
            )
        )

    def _report_error(self, exc):
        """Report an error outside a callback the way Scrapy reports callback
        errors: ERROR log, spider_exceptions stat, and the spider_error signal
        that scrapy_sentry_errors forwards to Sentry."""
        self.logger.error(str(exc))
        self.crawler.stats.inc_value(
            f"spider_exceptions/{exc.__class__.__name__}", spider=self
        )
        self.crawler.signals.send_catch_log(
            signal=signals.spider_error,
            failure=Failure(exc),
            response=None,
            spider=self,
        )

    def _request_meetings_list(self, response):
        yield self._boarddocs_post(
            "BD-GetMeetingsList",
            body=f"current_committee_id={self.boarddocs_committee_id}",
            callback=self._parse_meetings_list,
        )

    def _parse_meetings_list(self, response):
        try:
            meetings = response.json()
        except ValueError as e:
            # Reported at close as "0 meetings parsed"
            self.logger.warning("BoardDocs meetings list is not JSON: %s", e)
            return
        for item in self._get_clean_meetings(meetings):
            meeting_id = item["unique"]
            yield self._boarddocs_post(
                "BD-GetMeeting",
                body=f"current_committee_id={self.boarddocs_committee_id}&id={meeting_id}",  # noqa
                callback=self.parse,
                cb_kwargs={"meeting_id": meeting_id},
            )

    def _get_clean_meetings(self, data):
        """Keep list entries from the last 3 years and all future ones."""
        cutoff = datetime.now(self.tzinfo).date() - self.lookback
        clean = []
        for item in data:
            if not item or not item.get("unique"):
                # The list includes empty entries
                continue
            try:
                item_date = datetime.strptime(item["numberdate"], "%Y%m%d").date()
            except (KeyError, TypeError, ValueError):
                self.logger.warning(
                    "Skipping BoardDocs meeting %s with an invalid date: %r",
                    item.get("unique"),
                    item.get("numberdate"),
                )
                continue
            if item_date >= cutoff:
                clean.append(item)
        return clean

    def parse(self, response, meeting_id=None):
        """Parse one meeting's BD-GetMeeting HTML into a Meeting."""
        if re.search(r"no access", response.text, re.I):
            # BoardDocs returns a "No access" page for meetings that are not
            # public yet
            self.logger.warning(
                "BoardDocs meeting %s returned 'No access', skipping", meeting_id
            )
            return
        meeting_date = self._parse_date(response)
        if meeting_date is None:
            return
        raw_title = " ".join(response.css(".meeting-name ::text").getall()).strip()
        title = self._parse_title(raw_title)
        lines = self._parse_description_lines(response)
        start, end = self._parse_start_end(lines, meeting_date)
        meeting_page = self._parse_meeting_page(response, meeting_id)
        meeting = Meeting(
            title=title,
            description=" ".join(lines),
            classification=self._parse_classification(title),
            start=start,
            end=end,
            all_day=False,
            time_notes=self._parse_time_notes(start),
            location=self._parse_location(lines),
            links=[{"title": "Agenda", "href": meeting_page}] if meeting_page else [],
            source=meeting_page or self.source_url,
        )
        meeting["status"] = self._get_status(meeting, text=raw_title)
        meeting["id"] = self._get_id(meeting)
        self._meetings_parsed += 1
        yield meeting

    def _parse_date(self, response):
        date_text = response.css(".meeting-date::text").get(default="").strip()
        try:
            return datetime.strptime(date_text, "%A, %B %d, %Y").date()
        except ValueError:
            self.logger.warning("Skipping meeting with unparseable date %r", date_text)
            return None

    def _parse_title(self, raw_title):
        return self.title_status_re.sub("", raw_title).strip()

    def _parse_description_lines(self, response):
        """Each line of the description. Lines break only at <br> and block
        tags, so inline formatting such as "<b>6:00</b> PM" stays one line.
        Newlines in the HTML source are only spaces, as in a browser."""
        html = response.css(".meeting-description").get() or ""
        html = self.line_break_re.sub("\n", re.sub(r"\s+", " ", html))
        lines = []
        for text in replace_entities(remove_tags(html)).split("\n"):
            line = re.sub(r"\s+", " ", text).strip()
            if line:
                lines.append(line)
        return lines

    def _parse_classification(self, title):
        if self.committee_re.search(title):
            return COMMITTEE
        return BOARD

    def _parse_start_end(self, lines, meeting_date):
        """Read the start (and end, for ranges like "5:30 PM-8:00 PM" or
        "5:30-7:30 P.M.") from the first line that has a time with a.m./p.m.
        Without one the start is midnight, flagged in time_notes."""
        time_line = next(
            (line for line in lines if self.meridiem_re.search(line)), None
        )
        match = self.time_re.search(time_line) if time_line else None
        start_time = end_time = None
        if match:
            hour, minute, meridiem, end_hour, end_minute, end_meridiem = match.groups()
            try:
                # In "5:30-7:30 P.M." the start borrows the end's P.M.
                start_time = self._to_time(hour, minute, meridiem or end_meridiem)
                if end_hour:
                    end_time = self._to_time(
                        end_hour, end_minute, end_meridiem or meridiem
                    )
            except ValueError:
                self.logger.warning("Unparseable meeting time in %r", time_line)
        if start_time is None:
            self.logger.warning(
                "No start time found for meeting on %s; using 00:00", meeting_date
            )
            return datetime.combine(meeting_date, time(0, 0)), None
        start = datetime.combine(meeting_date, start_time)
        end = None
        if end_time and end_time > start_time:
            end = datetime.combine(meeting_date, end_time)
        return start, end

    def _to_time(self, hour, minute, meridiem):
        if not meridiem:
            raise ValueError(f"no a.m./p.m. for {hour}:{minute}")
        return datetime.strptime(
            f"{hour}:{minute} {meridiem.upper()}M", "%I:%M %p"
        ).time()

    def _parse_time_notes(self, start):
        """A 00:00 start means no time could be read (the board never meets
        at midnight)."""
        if start.time() == time(0, 0):
            return "Please check the meeting attachment for start time details."
        return ""

    def _parse_location(self, lines):
        """Find the street line and the "COLUMBUS, OH 43215" line in the
        description. Lines above the street that name a building become the
        location name; lines between the street and the city (such as
        "ASSEMBLY ROOM") are the room."""
        street_index = next(
            (
                i
                for i, line in enumerate(lines)
                if self.street_re.match(line) and not self.meridiem_re.search(line)
            ),
            None,
        )
        if street_index is None:
            self.logger.warning("No street address in description: %r", lines)
            return {"name": "TBD", "address": ""}

        street = lines[street_index]
        street_rooms = [
            self._clean_room(match.group())
            for match in self.street_room_re.finditer(street)
        ]
        street = self.street_room_re.sub("", street).strip()
        city_index = street_index
        if not self.city_re.search(street):
            city_index = next(
                (
                    i
                    for i in range(street_index + 1, len(lines))
                    if self.city_re.search(lines[i])
                ),
                None,
            )
        names = [
            line.title()
            for line in lines[:street_index]
            if not self.generic_name_re.match(line)
        ]
        rooms = street_rooms
        if city_index is not None:
            rooms += [
                self._clean_room(line) for line in lines[street_index + 1 : city_index]
            ]
        else:
            self.logger.warning("No city line after street %r", street)

        known = self._known_location(street)
        if known:
            name = ", ".join(names) or known["name"]
            address = known["address"]
        else:
            name = ", ".join(names) or self.agency
            parts = [street]
            if city_index is not None and city_index != street_index:
                parts.append(lines[city_index])
            address = self._format_address(", ".join(parts))
            if not self.zip_re.search(address):
                self.logger.warning("Address without a ZIP code: %r", address)
        if rooms:
            name = ", ".join([name] + rooms)
        return {"name": name, "address": address}

    def _clean_room(self, text):
        """ "/ROOM 825", "RM 825" and "IN-PERSON (ROOM 825)" all become
        "Room 825"."""
        room = re.sub(r"^\W+|\W+$", "", text)
        room = re.sub(r"^IN-PERSON\s*\((.*)$", r"\1", room, flags=re.I)
        room = re.sub(r"^RM\b\.?", "ROOM", room, flags=re.I)
        return room.title()

    def _format_address(self, address):
        """Title-case an address the source wrote in capitals and shorten the
        state: "2555 BRIGGS ROAD, COLUMBUS, OHIO 43223" becomes "2555 Briggs
        Road, Columbus, OH 43223"."""
        address = re.sub(r"\bOHIO\b", "OH", address, flags=re.I).title()
        return re.sub(r"\bOh\b", "OH", address)

    def _known_location(self, street):
        key = re.sub(r"[.,]", "", street.upper())
        key = re.sub(r"\bEAST\b", "E", key)
        key = re.sub(r"\bSOUTH\b", "S", key)
        key = re.sub(r"\s+", " ", key)
        for prefix, location in self.known_locations.items():
            if key.startswith(prefix):
                return location
        return None

    def _parse_meeting_page(self, response, meeting_id):
        """The meeting's own BoardDocs page, which holds the agenda. It is
        both the meeting's source and its agenda link. The page's "Copy URL"
        button carries this goto URL; without it the URL is built from the
        meeting id."""
        href = response.css("button.url::attr(data-clipboard-text)").get()
        if not href and meeting_id:
            href = self.meeting_url.format(meeting_id=meeting_id)
        if not href:
            self.logger.warning("No BoardDocs page URL for meeting %s", meeting_id)
        return href

    def _get_status(self, item, text=""):
        """Decide cancellation from the title and status text only.

        Descriptions on this board get copied between meetings: in November
        2026 the Oct 20, 2026 meeting carried "Meeting has been postponed to
        February 3, 2026" from the Jan 27 meeting, which the core check would
        read as a cancellation.
        """
        meeting_text = " ".join([item.get("title", ""), text]).lower()
        if any(word in meeting_text for word in ["cancel", "rescheduled", "postpone"]):
            return CANCELLED
        if item["start"] < datetime.now(self.tzinfo).replace(tzinfo=None):
            return PASSED
        return TENTATIVE
