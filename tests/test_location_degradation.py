"""
test_location_degradation.py — A dead weather feed must not blank the whole
location section.

Background: on 2026-08-07 the first spin-up run had weather time out with no
cache to fall back on, so `_fetch_weather` returned `{}` — its documented
"failed (no cache)" contract. `_write_location` then handed that empty dict
straight to `weather.format_weather_md`, which indexes `w["weather_code"]`
unguarded and raised KeyError. The section writer caught it and reported
`failed`, so the note lost its address, its air quality, AND its local news —
three working data sources discarded because a fourth was missing.

Two separate defects, tested separately:

  1. `format_weather_md` raises on a payload it will predictably be handed.
     A formatter's job is to render what it has.
  2. `_write_location` treats weather as load-bearing for the entire section.
     Cross-section coupling: unrelated content should survive.

Note the call sites at spin_up.py:1241 and :1307 already say
`w.get('weather_code', 0)` — someone hardened the cheap ones and left the
formatter, which is the one that actually reaches the note.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_location_degradation.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import spin_up
import weather

# A complete payload, exactly the shape weather.fetch() returns.
FULL = {
    "location": "Chico, CA",
    "lat": 39.73, "lon": -121.84,
    "fetched_at": "2026-08-08T02:02:08",
    "current_temp_f": 77.1, "feels_like_f": 75.4,
    "humidity_pct": 38, "wind_mph": 6.2,
    "weather_code": 0,
    "high_f": 99.1, "low_f": 61.0,
    "precip_pct": 0,
    "sunrise": "2026-08-08T06:12", "sunset": "2026-08-08T20:11",
}

# Faithful to air_quality.fetch() and local_news.fetch() — the keys their
# formatters actually index. Stubs that invent key names would make the
# isolation tests below pass for the wrong reason.
AIR = {"fetched_at": "2026-08-08T02:02:08", "us_aqi": 46,
       "pm2_5": 7.4, "pm10": 12.1, "ozone": 61.0}

NEWS = {
    "query": "Chico California",
    "fetched_at": "2026-08-08T02:02:08",
    "count": 1,
    "stories": [{"title": "Chico council votes on water",
                 "link": "https://example.test/water",
                 "source": "Enterprise-Record",
                 "published": "2026-08-07"}],
}


class TestFormatterSurvivesMissingData(unittest.TestCase):
    """The formatter must render whatever subset it was given."""

    def test_empty_dict_does_not_raise(self):
        """The exact 08-07 payload: _fetch_weather's 'failed (no cache)' return."""
        try:
            out = weather.format_weather_md({})
        except Exception as e:
            self.fail(f"empty payload raised {type(e).__name__}: {e}")
        self.assertIsInstance(out, str)
        self.assertTrue(out.strip(), "empty payload produced an empty string")

    def test_empty_dict_says_it_is_unavailable(self):
        """Silence is the bug from the arXiv saga. Absent data must announce itself."""
        out = weather.format_weather_md({}).lower()
        self.assertIn("unavailable", out,
                      "a missing feed rendered without saying it was missing")

    def test_missing_weather_code_alone(self):
        """The literal KeyError from the traceback, in isolation."""
        partial = {k: v for k, v in FULL.items() if k != "weather_code"}
        out = weather.format_weather_md(partial)
        self.assertIn("77", out, "dropped the temperature it actually had")

    def test_partial_payload_keeps_the_fields_it_has(self):
        """Degrading must not mean discarding. Render the known, skip the unknown."""
        partial = {"current_temp_f": 77.1, "weather_code": 0,
                   "fetched_at": "2026-08-08T02:02:08"}
        out = weather.format_weather_md(partial)
        self.assertIn("Clear", out)
        self.assertIn("77", out)

    def test_full_payload_is_unchanged(self):
        """Negative control: hardening must not alter the normal-day render."""
        out = weather.format_weather_md(FULL)
        for expected in ["Clear", "77", "feels 75", "hi 99", "lo 61",
                         "0% precip", "wind 6", "humidity 38",
                         "06:12", "20:11", "Open-Meteo"]:
            self.assertIn(expected, out, f"normal-day render lost {expected!r}")


class TestLocationSectionDecoupling(unittest.TestCase):
    """Weather is one line of four. It must not be able to veto the other three."""

    def _write(self, w):
        """Run _write_location against a stubbed note, return (ok, status, md)."""
        captured = {}

        def fake_write_section(section, content, **kw):
            captured["section"] = section
            captured["md"] = content
            return {"ok": True}

        with mock.patch.object(spin_up.daily_note, "section_status",
                               return_value={"location": "empty"}):
            with mock.patch.object(spin_up.daily_note, "write_section",
                                   side_effect=fake_write_section):
                ok, status = spin_up._write_location(w, NEWS, aq=AIR)
        return ok, status, captured.get("md", "")

    def test_dead_weather_still_writes_the_section(self):
        ok, status, md = self._write({})
        self.assertTrue(ok, f"section was abandoned: {status}")
        self.assertTrue(md, "nothing was written to the note")

    def test_dead_weather_preserves_the_other_three_sources(self):
        """Address, air quality, and local news are independent of the weather API."""
        _, _, md = self._write({})
        self.assertIn("Chico, CA", md, "lost the address")
        self.assertIn("39.73", md, "lost the coordinates")
        self.assertIn("Air quality", md, "lost air quality, which fetched fine")
        self.assertIn("water", md, "lost local news, which fetched fine")

    def test_dead_weather_is_visible_in_the_status_line(self):
        """A degraded write must not report a clean 'written'."""
        _, status, _ = self._write({})
        self.assertNotEqual(status, "written",
                            "a degraded section reported a clean status")
        self.assertIn("weather", status.lower())

    def test_healthy_weather_reports_a_clean_written(self):
        """Negative control: the normal path must stay quiet."""
        ok, status, md = self._write(FULL)
        self.assertTrue(ok)
        self.assertEqual(status, "written")
        self.assertIn("Clear", md)


if __name__ == "__main__":
    unittest.main()
