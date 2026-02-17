from city_scrapers_core.constants import NOT_CLASSIFIED, CITY_COUNCIL
from city_scrapers_core.items import Meeting
from city_scrapers_core.spiders import CityScrapersSpider

import json
import scrapy
from datetime import datetime
from dateutil.relativedelta import relativedelta


class ColumCityCouncilSpider(CityScrapersSpider):
    name = "colum_city_council"
    agency = "Columbus City Council"
    timezone = "America/Chicago"
    start_urls = ["https://www.columbus.gov/Government/City-Council/Meeting-Schedules-Agendas/City-Council-Meeting-Calendar"]
    calendar_api_url = ("https://www.columbus.gov/ocapi/calendars/getcalendaritems")
    attachments_calendar_url = "https://columbus.legistar.com/Calendar.aspx"
    source_url = "https://www.columbus.gov/Government/City-Council/Meeting-Schedules-Agendas/City-Council-Meeting-Calendar"

    custom_settings = {"ROBOTSTXT_OBEY": False}


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
                    "Origin": "https://www.columbus.gov",
                },
                body=json.dumps(
                    {
                        "LanguageCode":"en-US",
                        "Ids":["b485bcac-3066-4b86-9053-4f35f64a8097"],
                        "StartDate":"2025-01-01",
                        "EndDate":"2025-12-28"
                    }
                ),
                callback=self.parse,
                dont_filter=True,
            )


    def parse(self, response):
        """Parse calendar items and yield detail requests for valid meetings."""

        for item in response.css(".meetings"):
        #     meeting = Meeting(
        #         title=self._parse_title(item),
        #         description=self._parse_description(item),
        #         classification=CITY_COUNCIL,
        #         start=self._parse_start(item),
        #         end=self._parse_end(item),
        #         all_day=self._parse_all_day(item),
        #         time_notes=self._parse_time_notes(item),
        #         location=self._parse_location(item),
        #         links=self._parse_links(item),
        #         source=self._parse_source(response),
        #     )

        #     meeting["status"] = self._get_status(meeting)
        #     meeting["id"] = self._get_id(meeting)

            yield None

    def _parse_title(self, item):
        """Parse or generate meeting title."""
        return ""

    def _parse_description(self, item):
        """Parse or generate meeting description."""
        return ""

    def _parse_classification(self, item):
        """Parse or generate classification from allowed options."""
        return NOT_CLASSIFIED

    def _parse_start(self, item):
        """Parse start datetime as a naive datetime object."""
        return None

    def _parse_end(self, item):
        """Parse end datetime as a naive datetime object. Added by pipeline if None"""
        return None

    def _parse_time_notes(self, item):
        """Parse any additional notes on the timing of the meeting"""
        return ""

    def _parse_all_day(self, item):
        """Parse or generate all-day status. Defaults to False."""
        return False

    def _parse_location(self, item):
        """Parse or generate location."""
        return {
            "address": "",
            "name": "",
        }

    def _parse_links(self, item):
        """Parse or generate links."""
        return [{"href": "", "title": ""}]

    def _parse_source(self, response):
        """Parse or generate source."""
        return response.url
