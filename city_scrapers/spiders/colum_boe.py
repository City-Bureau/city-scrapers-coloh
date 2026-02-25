import random
import re
from datetime import datetime

import scrapy
from city_scrapers_core.constants import BOARD
from city_scrapers_core.items import Meeting
from city_scrapers_core.spiders import CityScrapersSpider
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta


class ColumBoeSpider(CityScrapersSpider):
    name = "colum_boe"
    agency = "Columbus Board of Education"
    timezone = "America/Chicago"
    api_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetMeetingsList?open&0.{random_digit}"  # noqa
    detail_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetMeeting?open&0.{random_digit}"  # noqa
    get_agenda_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/BD-GetAgenda?open&0.{random_digit}"  # noqa
    agenda_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/goto?open&id={attachment_id}"  # noqa

    base_url = "https://go.boarddocs.com/oh/columbus/Board.nsf/Public"

    boarddocs_committee_id = "A9HCVU32F33A"

    random_digit = random.randint(10**14, 10**15 - 1)

    custom_settings = {
        "ROBOTSTXT_OBEY": False,
    }

    def start_requests(self):
        yield scrapy.Request(
            url=self.api_url.format(random_digit=self.random_digit),
            method="POST",
            body=f"current_committee_id={self.boarddocs_committee_id}",
            callback=self._get_meeting_detail,
        )

    def _get_meeting_detail(self, response):
        meetings = response.json()
        filtered_meetings = self._get_clean_meetings(meetings)

        for meeting in filtered_meetings:
            meeting_id = meeting.get("unique")
            yield scrapy.Request(
                url=self.detail_url.format(random_digit=self.random_digit),
                method="POST",
                body=f"current_committee_id={self.boarddocs_committee_id}&id={meeting_id}",  # noqa
                meta={"meeting_id": meeting_id},
                callback=self._get_agenda,
            )

    def _get_clean_meetings(self, data):
        """
        Cleans the data by removing any items with a date older than
        3 years.
        """
        today = datetime.today()
        past_range = today - relativedelta(years=3)

        filtered_data = []
        for item in data:
            if not item:
                # Some items can be empty
                continue
            item_date = datetime.strptime(item["numberdate"], "%Y%m%d").date()
            if item_date > past_range.date():
                item["start_date"] = item_date
                filtered_data.append(item)
        return filtered_data

    def _get_agenda(self, response):
        raw_description = response.css(".meeting-description::text").getall()
        meeting_id = response.meta["meeting_id"]
        yield scrapy.Request(
            url=self.get_agenda_url.format(random_digit=self.random_digit),
            method="POST",
            body=f"current_committee_id={self.boarddocs_committee_id}&id={meeting_id}",
            meta={"detail_response": response, "raw_description": raw_description},
            callback=self.parse,
        )

    def parse(self, response):
        detail_response = response.meta["detail_response"]
        raw_description = response.meta["raw_description"]
        location, has_location = self._parse_location(detail_response)
        start_time, estimated_time = self._parse_start(raw_description, detail_response)
        meeting = Meeting(
            title=self._parse_title(detail_response),
            description=" ".join(raw_description).strip(),
            classification=BOARD,
            start=start_time,
            end=None,
            all_day=False,
            time_notes=self._parse_time_notes(has_location, estimated_time),
            location=location,
            links=self._parse_links(response),
            source=self.base_url,
        )

        meeting["status"] = self._get_status(meeting)
        meeting["id"] = self._get_id(meeting)

        yield meeting

    def _parse_title(self, item):
        title = item.css(".meeting-name::text").get()
        return title

    def _parse_start(self, raw_description, detail_response):
        date = detail_response.css(".meeting-date::text").get()
        description = " ".join(raw_description).lower()
        time_match = re.search(r"(\d{1,2}:\d{2})\s*([AaPp]\.?[Mm]\.?)", description)

        if time_match:
            time_str = f"{time_match.group(1)} {time_match.group(2).replace('.', '')}"
            return parse(f"{date} {time_str}"), False

        return parse(f"{date} 9:00 AM"), True

    def _parse_time_notes(self, has_location, estimated_time):
        missing = []
        if not has_location:
            missing.append("location")
        if estimated_time:
            missing.append("time")
        if missing:
            return f"Please refer to the meeting description or agenda for {' and '.join(missing)} details."  # noqa
        return ""

    def _parse_location(self, detail_response):
        title_location = self._parse_title(detail_response).lower()
        if "board" in title_location and "special" not in title_location:
            return {
                "name": "COLUMBUS CITY SCHOOLS",
                "address": "3700 S. HIGH ST. COLUMBUS, OH 43207",
            }, True
        return {
            "name": "TBD",
            "address": "",
        }, False

    def _parse_links(self, item):
        agenda_id = item.css("li.XXXXXXui-corner-all::attr(unique)").get()
        return (
            [
                {
                    "title": "Agenda",
                    "href": self.agenda_url.format(attachment_id=agenda_id),
                }
            ]
            if agenda_id
            else []
        )
