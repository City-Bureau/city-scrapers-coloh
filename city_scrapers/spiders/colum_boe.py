import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

import scrapy
from city_scrapers_core.constants import BOARD, CANCELLED, COMMITTEE, PASSED, TENTATIVE
from city_scrapers_core.items import Meeting
from city_scrapers_core.spiders import CityScrapersSpider
from dateutil.relativedelta import relativedelta
from scrapy import signals
from scrapy.http import HtmlResponse
from scrapy_playwright.page import PageMethod
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
    # Links a meeting or an agenda item by its BoardDocs unique id
    goto_url = (
        "https://go.boarddocs.com/oh/columbus/Board.nsf/goto?open&id={unique}"  # noqa
    )
    boarddocs_committee_id = "A9HCVU32F33A"
    lookback = relativedelta(years=3)
    # Milliseconds between BoardDocs calls. DOWNLOAD_DELAY does not apply,
    # because the calls are made from inside the browser page, not by Scrapy.
    fetch_delay_ms = 1000
    # Nothing else limits a call: page.evaluate() and the browser's fetch()
    # have no deadline, so one stalled response would hang the whole run
    fetch_timeout_ms = 30000
    # Stop calling BoardDocs once this many calls in a row have failed
    # (blocked or down), so a bad run ends instead of failing every remaining
    # call. Checked before each meeting, so the stop comes after a whole
    # agenda and meeting pair.
    max_consecutive_failures = 5

    # BoardDocs sits behind CloudFront, which now blocks plain HTTP clients
    # (Scrapy, Postman) even with a browser User-Agent: it answers 200 with a
    # blank body and "X-Cache: Error from cloudfront" (Nov 2026). So the
    # public page is loaded in a real browser with scrapy-playwright, and the
    # BoardDocs API is called with fetch() from inside that page, the way the
    # page's own scripts do. The browser must send its own headers: with
    # Scrapy's headers CloudFront answers 403 even in Firefox, and headless
    # Chromium gets the blank 200.
    custom_settings = {
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        # https only: each scheme gets its own handler, and each handler
        # launches its own browser. BoardDocs is https only.
        "DOWNLOAD_HANDLERS": {
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_TYPE": "firefox",
        "PLAYWRIGHT_PROCESS_REQUEST_HEADERS": None,
        "ROBOTSTXT_OBEY": False,
        "FEED_EXPORT_ENCODING": "utf-8",
    }

    # Runs inside the loaded public page as a PageMethod: reads the meetings
    # list, then the agenda and details of each meeting since the cutoff, one
    # call at a time. Every call's status and raw text are returned for the
    # Python side to check and parse (scrapy-playwright's own response body
    # is the rendered page, which wraps JSON in HTML). The random number in
    # each URL is the page's own cache-buster. Each call is aborted after
    # timeoutMs, including reading its body, and returned as a failed result.
    # After maxFailures failed calls in a row the loop stops and says so in
    # "stopped".
    fetch_all_js = """async ({
        baseUrl, committeeId, cutoff, delayMs, timeoutMs, maxFailures,
    }) => {
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        let failuresInRow = 0;
        const post = async (endpoint, body) => {
            const cacheBuster = String(Math.floor(1e14 + Math.random() * 9e14));
            const url = baseUrl
                .replace("{endpoint}", endpoint)
                .replace("{cache_buster}", cacheBuster);
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            let result;
            try {
                const r = await fetch(url, {
                    method: "POST",
                    body,
                    headers: {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    signal: controller.signal,
                });
                result = {url, status: r.status, text: await r.text()};
            } catch (e) {
                result = {url, status: 0, text: "", error: String(e)};
            } finally {
                clearTimeout(timer);
            }
            const failed = result.status !== 200 || !result.text.trim();
            failuresInRow = failed ? failuresInRow + 1 : 0;
            return result;
        };
        const list = await post(
            "BD-GetMeetingsList", "current_committee_id=" + committeeId
        );
        const meetings = {};
        let items;
        try {
            items = JSON.parse(list.text);
        } catch (e) {
            return {list, meetings};
        }
        if (!Array.isArray(items)) {
            return {list, meetings};
        }
        for (const item of items) {
            // Same entries as _get_clean_meetings keeps
            if (!item || !item.unique || !/^\\d{8}$/.test(item.numberdate)
                    || item.numberdate < cutoff) {
                continue;
            }
            if (failuresInRow >= maxFailures) {
                const stopped = `${failuresInRow} failed calls in a row`;
                return {list, meetings, stopped};
            }
            const body = `current_committee_id=${committeeId}&id=${item.unique}`;
            await sleep(delayMs);
            const agenda = await post("BD-GetAgenda", body);
            await sleep(delayMs);
            const meeting = await post("BD-GetMeeting", body);
            meetings[item.unique] = {agenda, meeting};
        }
        return {list, meetings};
    }"""

    # The board meets in two district buildings. The source writes their
    # addresses several ways ("270 E. STATE ST.", "270 E. STATE STREET",
    # "COLUMBUS, OHIO" with no ZIP), so they are matched on number and street
    # and given one complete address. A building the description names (such
    # as "SOUTHLAND CENTER") is kept; these names are only used when it names
    # none. Address-only descriptions at 3700 S. High say "COLUMBUS CITY
    # SCHOOLS", so that is the name used there.
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._failed_requests = 0
        self._meetings_parsed = 0
        self._cutoff = None

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._on_spider_closed, signal=signals.spider_closed)
        return spider

    def start_requests(self):
        """Load the public page in the browser and run fetch_all_js in it.
        scrapy-playwright closes the page afterwards."""
        cutoff = self._get_cutoff()
        yield scrapy.Request(
            self.source_url,
            callback=self._parse_results,
            errback=self._handle_error,
            dont_filter=True,
            meta={
                "playwright": True,
                "playwright_page_methods": {
                    "fetch_all": PageMethod(
                        "evaluate",
                        self.fetch_all_js,
                        {
                            "baseUrl": self.base_url,
                            "committeeId": self.boarddocs_committee_id,
                            "cutoff": cutoff.strftime("%Y%m%d"),
                            "delayMs": self.fetch_delay_ms,
                            "timeoutMs": self.fetch_timeout_ms,
                            "maxFailures": self.max_consecutive_failures,
                        },
                    ),
                },
            },
        )

    def _handle_error(self, failure):
        """A failed request (the public page not loading, or fetch_all_js
        failing) means missing meetings. It is counted and reported once when
        the spider closes."""
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

    def _to_response(self, endpoint, result):
        """Turn one call's result from fetch_all_js into an HtmlResponse, or
        None if it failed or came back blank (CloudFront's block); a failure
        is counted."""
        if result["status"] != 200 or not result["text"].strip():
            self._failed_requests += 1
            self.logger.warning(
                "BoardDocs %s returned %s with %d bytes (blocked by CloudFront?)%s",
                endpoint,
                result["status"],
                len(result["text"]),
                f": {result['error']}" if result.get("error") else "",
            )
            return None
        return HtmlResponse(
            url=result["url"], body=result["text"].encode("utf-8"), encoding="utf-8"
        )

    def _parse_results(self, response):
        """Parse what fetch_all_js read: the meetings list, then each meeting
        with its agenda."""
        results = response.meta["playwright_page_methods"]["fetch_all"].result
        listing = self._to_response("BD-GetMeetingsList", results["list"])
        if listing is None:
            return
        try:
            meetings = listing.json()
        except ValueError as e:
            # Reported at close as "0 meetings parsed"
            self.logger.warning("BoardDocs meetings list is not JSON: %s", e)
            return
        if results.get("stopped"):
            self.logger.warning(
                "Stopped reading BoardDocs after %s", results["stopped"]
            )
        not_read = []
        for item in self._get_clean_meetings(meetings):
            meeting_id = item["unique"]
            fetched = results["meetings"].get(meeting_id)
            if not fetched:
                not_read.append(meeting_id)
                continue
            # A failed agenda call is counted, but the meeting is still
            # parsed, without an agenda link, so it is not lost
            agenda = self._to_response("BD-GetAgenda", fetched["agenda"])
            agenda_id = self._parse_agenda_id(agenda, meeting_id)
            meeting = self._to_response("BD-GetMeeting", fetched["meeting"])
            if meeting is None:
                continue
            yield from self.parse(meeting, meeting_id=meeting_id, agenda_id=agenda_id)
        if not_read:
            # One line for all of them: after a stop this can be most meetings
            self._failed_requests += len(not_read)
            self.logger.warning(
                "%d BoardDocs meetings were not read: %s",
                len(not_read),
                ", ".join(not_read),
            )

    def _parse_agenda_id(self, agenda, meeting_id):
        """The agenda link points to the meeting's first agenda item, as in the
        original spider. A meeting with no agenda items yet gets no link."""
        if agenda is None:
            return None
        agenda_id = agenda.css("li.XXXXXXui-corner-all::attr(unique)").get()
        if not agenda_id:
            self.logger.info("No agenda items yet for meeting %s", meeting_id)
        return agenda_id

    def _get_cutoff(self):
        """The earliest meeting date kept, 3 years before the run started. It
        is set once per run so fetch_all_js and _get_clean_meetings use the
        same date even when a run crosses midnight."""
        if self._cutoff is None:
            self._cutoff = datetime.now(self.tzinfo).date() - self.lookback
        return self._cutoff

    def _get_clean_meetings(self, data):
        """Keep list entries from the last 3 years and all future ones. These
        are the meetings fetch_all_js reads, so each is expected in its
        results."""
        cutoff = self._get_cutoff()
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

    def parse(self, response, meeting_id=None, agenda_id=None):
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
            links=self._parse_links(agenda_id),
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

    def _parse_links(self, agenda_id):
        if not agenda_id:
            return []
        return [{"title": "Agenda", "href": self.goto_url.format(unique=agenda_id)}]

    def _parse_meeting_page(self, response, meeting_id):
        """The meeting's own BoardDocs page, used as its source. The page's
        "Copy URL" button carries this goto URL; without it the URL is built
        from the meeting id."""
        href = response.css("button.url::attr(data-clipboard-text)").get()
        if not href and meeting_id:
            href = self.goto_url.format(unique=meeting_id)
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
