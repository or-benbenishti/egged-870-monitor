"""LOCAL unit tests with a fake server. They prove the decision logic only, NOT that Egged works.
Live results against Egged are recorded separately in LIVE_TESTS.md."""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import monitor
import notify

TZ = ZoneInfo('Asia/Jerusalem')
NOW = dt.datetime(2026, 11, 20, 15, 0, tzinfo=TZ)


def local_epoch(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp())


def date_entry(day):
    return {'travelDateStr': day.strftime('%d/%m/%Y'), 'travelDateEpoch': local_epoch(day.year, day.month, day.day)}


def ride(day, hh, mm, line='870', up=52678, down=15332, deadline_offset_min=-10):
    board = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)
    return {'lineSign': line, 'makat8': '870200', 'motStopIdUp': up, 'motStopIdDown': down,
            'stopUpDt': board.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
            'stopUpTimeStr': f'{hh:02d}:{mm:02d}', 'travelDtEpoch': int(board.timestamp()) - 600,
            'maxBookingDtEpoch': int(board.timestamp()) + deadline_offset_min * 60}


class FakeServer:
    def __init__(self, dates, rides_by_epoch, fail=()):
        self.dates, self.rides, self.fail, self.calls = dates, rides_by_epoch, set(fail), []

    def __call__(self, url):
        self.calls.append(url)
        if '/date?' in url:
            if 'date' in self.fail:
                raise TimeoutError('timed out')
            return self.dates
        epoch = int(url.split('travelDate=')[1].split('&')[0])
        if epoch in self.fail:
            raise ConnectionError('reset')
        return self.rides.get(epoch, [])


SAT1, SAT2 = dt.date(2026, 11, 21), dt.date(2026, 11, 28)


class Dates(unittest.TestCase):
    def test_next_saturday(self):
        self.assertEqual(monitor.next_saturday(dt.date(2026, 11, 20)), SAT1)
        self.assertEqual(monitor.next_saturday(SAT1), SAT1)

    def test_two_saturdays_from_friday(self):
        self.assertEqual(monitor.upcoming_saturdays(NOW), [SAT1, SAT2])

    def test_saturday_before_and_after_departure(self):
        before = dt.datetime(2026, 11, 21, 19, 0, tzinfo=TZ)
        after = dt.datetime(2026, 11, 21, 19, 40, tzinfo=TZ)
        self.assertEqual(monitor.upcoming_saturdays(before), [SAT1, SAT2])
        self.assertEqual(monitor.upcoming_saturdays(after), [SAT2, dt.date(2026, 12, 5)])

    def test_dst_and_utc_input(self):
        # 2027-03-27 is a Saturday after Israel moved to UTC+3; input given in UTC
        now_utc = dt.datetime(2027, 3, 27, 16, 0, tzinfo=dt.timezone.utc)  # 19:00 local
        self.assertEqual(monitor.upcoming_saturdays(now_utc)[0], dt.date(2027, 3, 27))

    def test_rides_url_uses_local_midnight_epoch(self):
        self.assertIn('travelDate=1795212000', monitor.rides_url(local_epoch(2026, 11, 21)))


class Decision(unittest.TestCase):
    def srv(self, **kw):
        day = kw.pop('day', SAT1)
        rides = kw.pop('rides', [ride(day, 19, 5)])
        dates = kw.pop('dates', [date_entry(dt.date(2026, 11, 22)), date_entry(day)])
        return FakeServer(dates, {local_epoch(day.year, day.month, day.day): rides}, **kw)

    def test_open(self):
        r = monitor.check_trip(SAT1, now=NOW, fetch=self.srv())
        self.assertEqual(r['status'], 'OPEN')
        self.assertEqual(r['boarding_time'], '19:05')

    def test_date_not_offered_is_not_open(self):
        r = monitor.check_trip(SAT1, now=NOW, fetch=self.srv(dates=[date_entry(dt.date(2026, 11, 22))]))
        self.assertEqual(r['status'], 'NOT_OPEN')

    def test_wrong_time_wrong_line_wrong_stop_not_open(self):
        for rides in ([ride(SAT1, 14, 40)], [ride(SAT1, 19, 5, line='871')], [ride(SAT1, 19, 5, up=1)]):
            self.assertEqual(monitor.check_trip(SAT1, now=NOW, fetch=self.srv(rides=rides))['status'], 'NOT_OPEN')

    def test_deadline_passed_not_open(self):
        late = dt.datetime(2026, 11, 21, 19, 0, tzinfo=TZ)
        self.assertEqual(monitor.check_trip(SAT1, now=late, fetch=self.srv())['status'], 'NOT_OPEN')

    def test_technical_failures_are_unknown(self):
        cases = [self.srv(fail={'date'}), self.srv(dates=[]), self.srv(dates={'error': 1}),
                 self.srv(rides=[]), self.srv(fail={local_epoch(2026, 11, 21)}), self.srv(rides='oops')]
        for s in cases:
            self.assertEqual(monitor.check_trip(SAT1, now=NOW, fetch=s)['status'], 'UNKNOWN')

    def test_picks_closest_ride(self):
        r = monitor.check_trip(SAT1, now=NOW, fetch=self.srv(rides=[ride(SAT1, 18, 50), ride(SAT1, 19, 10)]))
        self.assertEqual(r['boarding_time'], '19:10')

    def test_both_saturdays_checked_if_one_fails_and_one_dates_request(self):
        dates = [date_entry(SAT1), date_entry(SAT2)]
        s = FakeServer(dates, {local_epoch(2026, 11, 21): [ride(SAT1, 19, 5)],
                               local_epoch(2026, 11, 28): [ride(SAT2, 19, 5)]}, fail={local_epoch(2026, 11, 21)})
        rep = monitor.run([SAT1, SAT2], now=NOW, fetch=s)
        self.assertEqual([r['status'] for r in rep['results']], ['UNKNOWN', 'OPEN'])
        self.assertEqual(sum('/date?' in c for c in s.calls), 1)


class Notify(unittest.TestCase):
    def run_notify(self, report, existing, extra_args=()):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, 'r.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(report, f)
            env = {'GITHUB_REPOSITORY': 't/r', 'GH_TOKEN': 'x', 'TELEGRAM_BOT_TOKEN': 'x', 'TELEGRAM_CHAT_ID': '1'}
            with patch.dict(os.environ, env), \
                    patch('notify.existing_titles', return_value=set(existing)), \
                    patch('notify.api_request') as gh, patch('notify.telegram_send') as tg:
                notify.main(['--report', path, *extra_args])
            return gh, tg

    future = dt.date.today() + dt.timedelta(days=3)

    def test_only_open_alerts_once_per_date(self):
        d1, d2 = self.future.isoformat(), (self.future + dt.timedelta(days=7)).isoformat()
        rep = {'results': [{'date': d1, 'status': 'OPEN'}, {'date': d2, 'status': 'OPEN'},
                           {'date': d2, 'status': 'NOT_OPEN'}, {'date': d1, 'status': 'UNKNOWN'}]}
        gh, tg = self.run_notify(rep, existing=[f'[EGGED 870 OPEN] {d1}'])
        self.assertEqual(tg.call_count, 1)
        self.assertIn(dt.date.fromisoformat(d2).strftime('%d/%m/%Y'), tg.call_args.args[2])
        self.assertEqual(gh.call_count, 1)

    def test_rerun_does_not_duplicate(self):
        d = self.future.isoformat()
        gh, tg = self.run_notify({'results': [{'date': d, 'status': 'OPEN'}]}, existing=[f'[EGGED 870 OPEN] {d}'])
        self.assertEqual(tg.call_count + gh.call_count, 0)

    def test_not_open_and_unknown_never_alert(self):
        d = self.future.isoformat()
        gh, tg = self.run_notify({'results': [{'date': d, 'status': 'NOT_OPEN'}, {'date': d, 'status': 'UNKNOWN'}]}, [])
        self.assertEqual(tg.call_count + gh.call_count, 0)

    def test_dry_run_contacts_nothing(self):
        gh, tg = self.run_notify({'results': [{'date': self.future.isoformat(), 'status': 'OPEN'}]}, [], ['--dry-run'])
        self.assertEqual(tg.call_count + gh.call_count, 0)

    def test_past_date_ignored(self):
        gh, tg = self.run_notify({'results': [{'date': '2020-01-04', 'status': 'OPEN'}]}, [])
        self.assertEqual(tg.call_count, 0)


if __name__ == '__main__':
    unittest.main()
