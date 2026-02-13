import json
import re
from datetime import datetime

import scrapy
from city_scrapers_core.constants import COMMISSION
from city_scrapers_core.items import Meeting
from city_scrapers_core.spiders import CityScrapersSpider
from dateutil.relativedelta import relativedelta


class ColumFranklinBocSpider(CityScrapersSpider):
    """Spider for Franklin County Board of Commissioners meetings.

    Uses a two-step API pattern:
    1. POST to calendar API to get meeting items
    2. GET detail endpoint for each meeting to extract location, links, etc.
    """

    name = "colum_franklin_boc"
    agency = "Franklin County Board of Commissioners"
    timezone = "America/New_York"
    calendar_api_url = (
        "https://www.franklincountyohio.gov/ocapi/calendars/getcalendaritems"
    )
    detail_url = (
        "https://www.franklincountyohio.gov/OCServiceHandler.axd"
        "?url=ocsvc/Public/meetings/documentrenderer&cvid={}"
    )
    calendar_id = "90a168fd-1df9-44fa-8a0e-84d86cb1b0e4"
    source_url = (
        "https://www.franklincountyohio.gov/County-Government/"
        "About-Our-Community/Public-Meetings-and-Agendas"
    )
    custom_settings = {"ROBOTSTXT_OBEY": False}

    # Matches actual meeting names (Session, Hearing, Meeting)
    # and filters out holidays and community events
    MEETING_NAME_RE = re.compile(r"(Session|Hearing|Meeting)", re.IGNORECASE)

    def start_requests(self):
        """POST requests to fetch calendar items, split by year.

        The API does not support cross-year date ranges, so we issue
        one request per calendar year covering 2 months back through
        12 months forward.
        """
        now = datetime.now()
        start = now - relativedelta(months=2)
        end = now + relativedelta(months=12)

        for year in range(start.year, end.year + 1):
            year_start = f"{year}-01-01"
            year_end = f"{year}-12-31"

            yield scrapy.Request(
                url=self.calendar_api_url,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://www.franklincountyohio.gov",
                },
                body=json.dumps(
                    {
                        "LanguageCode": "en-US",
                        "Ids": [self.calendar_id],
                        "StartDate": year_start,
                        "EndDate": year_end,
                    }
                ),
                callback=self.parse,
                dont_filter=True,
            )

    def parse(self, response):
        """Parse calendar items and yield detail requests for valid meetings."""
        data = response.json()

        for day in data.get("data", []):
            for item in day.get("Items", []):
                name = item.get("Name", "")

                # Filter out holidays and community events
                if not self.MEETING_NAME_RE.search(name):
                    continue

                meeting_id = item.get("Id")
                if not meeting_id:
                    continue

                yield scrapy.Request(
                    url=self.detail_url.format(meeting_id),
                    headers={
                        "Origin": "https://www.franklincountyohio.gov",
                    },
                    callback=self.parse_detail,
                    meta={
                        "title": name,
                        "datetime_str": item.get("DateTime", ""),
                    },
                )

    def parse_detail(self, response):
        """Parse meeting detail JSON/HTML to extract full meeting information."""
        data = response.json()

        status = data.get("_response_status", {}).get("status")
        if status != "Okay":
            return

        html = data.get("html", "")
        sel = scrapy.Selector(text=html)

        title = response.meta["title"]
        start = self._parse_start(response.meta["datetime_str"])

        meeting = Meeting(
            title=title,
            description=self._parse_description(sel),
            classification=COMMISSION,
            start=start,
            end=None,
            all_day=False,
            time_notes="",
            location=self._parse_location(sel),
            links=self._parse_links(sel),
            source=self.source_url,
        )

        meeting["status"] = self._get_status(meeting)
        meeting["id"] = self._get_id(meeting)

        yield meeting

    def _parse_start(self, datetime_str):
        """Parse datetime from format like '5/6/2025 9:00:00 AM'."""
        if not datetime_str:
            return None
        try:
            return datetime.strptime(datetime_str, "%m/%d/%Y %I:%M:%S %p")
        except ValueError:
            return None

    def _parse_description(self, sel):
        """Extract description from the first <p> in the meeting container."""
        desc = sel.css("div.meeting-container > p::text").get()
        return desc.strip() if desc else ""

    def _parse_location(self, sel):
        """Extract location name and address from meeting detail HTML."""
        address_div = sel.css("div.meeting-address")
        if not address_div:
            return {"name": "", "address": ""}

        paragraphs = address_div.css("p")
        if not paragraphs:
            return {"name": "", "address": ""}

        name = ""
        # The last <p> always contains the street address
        address_texts = paragraphs[-1].css("::text").getall()
        address = self._clean_address(address_texts)

        # If multiple <p> elements, the first may be a room/location name
        # (short text) or an instruction note (long text)
        if len(paragraphs) >= 2:
            first_text = paragraphs[0].css("::text").get("").strip()
            if len(first_text) < 80:
                name = first_text

        return {"name": name, "address": address}

    def _clean_address(self, texts):
        """Clean address text by removing 'View Map' and normalizing whitespace."""
        parts = []
        for t in texts:
            t = t.replace("\xa0", " ").strip()
            if t and "View Map" not in t:
                parts.append(t)
        raw = " ".join(parts)
        return re.sub(r"\s+", " ", raw).strip().rstrip(",")

    def _parse_links(self, sel):
        """Extract document links (agendas, minutes) from meeting detail HTML."""
        links = []
        for doc_div in sel.css("div.meeting-document"):
            title = doc_div.css("h3::text").get("").strip()
            href = doc_div.css("div.alt-formats a::attr(href)").get()
            if href:
                if not href.startswith("http"):
                    href = "https://www.franklincountyohio.gov" + href
                links.append({"href": href, "title": title or "Document"})
        return links
