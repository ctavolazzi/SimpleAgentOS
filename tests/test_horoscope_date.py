"""
test_horoscope_date.py — The daily reading is headed with the note's date.

Background: the horoscope feed dates its reading by its own server clock. An
evening spin-up on Wednesday 2026-10-07 (Pacific) got a reading dated
2026-10-08, and the note for Wednesday opened with "Scorpio · Thursday,
October 8". `fetch()` passed the feed's date through as `date`, and
`format_md` printed whatever `date` held.

The reading is whatever the feed currently serves; there is no way to ask it
for another day. So the header names the day the note is for, and a feed date
that differs is disclosed beside the source instead of replacing the header.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_horoscope_date.py -v
"""

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import horoscope


def _feed(date_str: str):
    """A urlopen stand-in returning the feed's real response shape."""
    body = json.dumps({"sign": "scorpio", "date": date_str,
                       "horoscope": "Mars and Uranus align. Seize the day."}).encode()
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return mock.patch.object(horoscope.urllib.request, "urlopen", return_value=resp)


class TestFetchKeepsBothDates(unittest.TestCase):
    def test_date_is_local_today_and_feed_date_is_kept(self):
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        with _feed(tomorrow):
            data = horoscope.fetch()
        self.assertEqual(data["date"], today)
        self.assertEqual(data["feed_date"], tomorrow)
        self.assertTrue(data["horoscope"])


class TestHeaderNamesTheNotesDay(unittest.TestCase):
    WED, THU = "2026-10-07", "2026-10-08"

    def _md(self, **over):
        data = {"sign": "scorpio", "date": self.WED, "feed_date": self.THU,
                "horoscope": "Mars and Uranus align.", "source": "ohmanda.com"}
        data.update(over)
        return horoscope.format_md(data)

    def test_header_uses_the_note_date(self):
        header = self._md().splitlines()[0]
        self.assertIn("Wednesday, October 7", header)
        self.assertNotIn("October 8", header)

    def test_a_different_feed_date_is_disclosed(self):
        md = self._md()
        self.assertIn("ohmanda.com", md)
        self.assertIn("October 8", md, "the feed's own date was dropped silently")

    def test_matching_dates_add_no_note(self):
        md = self._md(feed_date=self.WED)
        self.assertNotIn("feed dated", md)

    def test_payload_cached_before_this_change_still_renders(self):
        data = {"sign": "scorpio", "date": self.WED,
                "horoscope": "Mars and Uranus align.", "source": "ohmanda.com"}
        md = horoscope.format_md(data)
        self.assertIn("Wednesday, October 7", md.splitlines()[0])
        self.assertNotIn("feed dated", md)


if __name__ == "__main__":
    unittest.main()
