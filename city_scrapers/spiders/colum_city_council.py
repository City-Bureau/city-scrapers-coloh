import json
from collections import defaultdict
from datetime import datetime

import scrapy
from city_scrapers_core.constants import BOARD, CITY_COUNCIL, COMMITTEE, NOT_CLASSIFIED
from city_scrapers_core.items import Meeting
from city_scrapers_core.spiders import LegistarSpider
from dateutil.relativedelta import relativedelta


class ColumCityCouncilSpider(LegistarSpider):
    name = "colum_city_council"
    agency = "Columbus City Council"
    timezone = "America/New_York"
    start_urls = ["https://columbus.legistar.com/Calendar.aspx"]
    # Tell base class to also grab these link types
    link_types = ["Accessible Agenda", "Accessible Minutes", "Meeting Details"]

    calendar_api_url = "https://www.columbus.gov/ocapi/calendars/getcalendaritems"
    source_url = "https://www.columbus.gov/Government/City-Council/Meeting-Schedules-Agendas/City-Council-Meeting-Calendar"  # noqa

    location = {
        "name": "City Council Chambers, Rm 231",
        "address": "90 West Broad Street, Columbus, OH, 43215",
    }

    custom_settings = {"ROBOTSTXT_OBEY": False}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.since_year = 2003
        self.legistar_dates = set()  # store scraped legistar dates here

    def start_requests(self):
        # 1. Legistar historical data first
        yield scrapy.Request(
            self.start_urls[0],
            callback=self.parse,
        )

        # 2. Upcoming meetings, from calendar API

    def parse(self, response):
        # after legistar finishes, trigger upcoming request
        yield from super().parse(response)

        now = datetime.now()
        end = now + relativedelta(months=12)

        yield scrapy.Request(
            url=self.calendar_api_url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.columbus.gov",
            },
            body=json.dumps(
                {
                    "LanguageCode": "en-US",
                    "Ids": ["b485bcac-3066-4b86-9053-4f35f64a8097"],
                    "StartDate": now.strftime("%Y-%m-%d"),
                    "EndDate": end.strftime("%Y-%m-%d"),
                }
            ),
            callback=self.parse_upcoming,
            dont_filter=True,
        )

    def parse_upcoming(self, response):
        result = response.json()
        for day in result.get("data", []):
            for item in day.get("Items", []):
                if item.get("Name") == "No Meetings":
                    continue
                start = self._parse_upcoming_start(item)
                if not start:
                    continue
                # skip if this date already exists in legistar data
                if start.date() in self.legistar_dates:
                    continue
                meeting = Meeting(
                    title=item.get("Name", "No Title"),
                    description="",
                    classification=self._parse_classification(item),
                    start=start,
                    end=None,
                    all_day=False,
                    time_notes="",
                    location=self.location,
                    links=[],
                    source=self.source_url,
                )
                meeting["status"] = self._get_status(meeting)
                meeting["id"] = self._get_id(meeting)
                yield meeting

    def _parse_upcoming_start(self, item):
        try:
            return datetime.strptime(item.get("DateTime", ""), "%m/%d/%Y %I:%M:%S %p")
        except (ValueError, TypeError):
            return None

    def parse_legistar(self, events):
        for event in events:
            start = self.legistar_start(event)
            if start:
                self.legistar_dates.add(start.date())
                name = event.get("Name", "")
                title = name if isinstance(name, str) else name.get("label", "No Title")
                meeting = Meeting(
                    title=title,
                    description="",
                    classification=self._parse_classification(event),
                    start=start,
                    end=None,
                    all_day=False,
                    time_notes="",
                    location=self.location,
                    links=self.legistar_links(event),  # base class method
                    source=self.legistar_source(event),  # base class method
                )
                meeting["status"] = self._get_status(meeting)
                meeting["id"] = self._get_id(meeting)
                yield meeting

    def _parse_legistar_events(self, response):
        events_table = response.css("table.rgMasterTable")[0]

        headers = []
        for header in events_table.css("th[class^='rgHeader']"):
            header_text = (
                " ".join(header.css("*::text").extract()).replace("&nbsp;", " ").strip()
            )
            header_inputs = header.css("input")
            if header_text:
                headers.append(header_text)
            elif len(header_inputs) > 0:
                headers.append(header_inputs[0].attrib["value"])
            else:
                headers.append(header.css("img")[0].attrib["alt"])

        events = []
        for row in events_table.css("tr.rgRow, tr.rgAltRow"):
            try:
                data = defaultdict(lambda: None)
                for header, field in zip(headers, row.css("td")):
                    field_text = (
                        " ".join(field.css("*::text").extract())
                        .replace("&nbsp;", " ")
                        .strip()
                    )
                    url = None
                    if len(field.css("a")) > 0:
                        link_el = field.css("a")[0]
                        if "onclick" in link_el.attrib and link_el.attrib[
                            "onclick"
                        ].startswith(("radopen('", "window.open", "OpenTelerikWindow")):
                            url = response.urljoin(
                                link_el.attrib["onclick"].split("'")[1]
                            )
                        elif "href" in link_el.attrib:
                            url = response.urljoin(link_el.attrib["href"])
                    if url:
                        # check URL content, not header name
                        if "View.ashx?M=IC" in url:
                            header = "iCalendar"
                            value = {"url": url}
                        else:
                            value = {"label": field_text, "url": url}
                    else:
                        value = field_text

                    data[header] = value

                ical_url = data.get("iCalendar", {}).get("url")
                if ical_url is None or ical_url in self._scraped_urls:
                    continue
                else:
                    self._scraped_urls.add(ical_url)

                events.append(dict(data))
            except Exception:
                pass

        return events

    def _parse_classification(self, item):
        name = item.get("Name", "")
        name_label = (name if isinstance(name, str) else name.get("label", "")).lower()
        if "committee" in name_label:
            return COMMITTEE
        if "board" in name_label:
            return BOARD
        if "council" in name_label:
            return CITY_COUNCIL
        return NOT_CLASSIFIED
