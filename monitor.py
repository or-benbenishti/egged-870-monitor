"""Read-only Egged reserved-seat availability monitor (line 870, Megiddo Jct -> Soroka/BGU).

How it decides (all requests are anonymous, read-only GETs that the public site itself makes,
none of them needs a cookie, a login or a reCAPTCHA token):

  1. GET .../date   -> the list of dates the site currently offers for booking on this route.
  2. GET .../ride   -> the rides offered on the requested date (only if the date is offered).
  3. Find the exact ride by line, boarding stop, alighting stop and local boarding date/time
     (stopUpDt, *not* travelDt, which is the departure from the line's first stop).
  4. Check the ride's booking deadline (maxBookingDtEpoch) is still in the future -
     the same check the site's own UI performs before letting you pick a ride.

OPEN      = all of the above passed.
NOT_OPEN  = the server answered normally and the date / ride is not offered or its deadline passed.
UNKNOWN   = the check itself failed or the answer was inconsistent; never treated as NOT_OPEN.

Deliberately NOT used: GET .../seat?travelDateTime=... ("setSeatsAuto"). On the site that call
*allocates and holds a real seat* (there is a matching DELETE to release it) and it requires a
reCAPTCHA token. Calling it hourly would hold seats from other passengers and would require
bypassing reCAPTCHA, so it is out of scope.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Jerusalem')
API = 'https://api.egged.co.il/prod/ReservedSeatsApp/mksm/app'
SITE = 'https://reserved-seats.egged.co.il/reserved-seats-app/order'

LINE = '870'
YESHUV_UP, STOP_UP = 586, 52678        # מגידו / צומת מגידו
YESHUV_DOWN, STOP_DOWN = 9000, 15332   # באר שבע / סורוקה - אוניברסיטת בן גוריון
PASSENGERS = 1
DEFAULT_TIME = '19:05'
TOLERANCE_MIN = 20       # boarding time may drift a little; never match another ride of the day
DEPARTED_GRACE_MIN = 30  # after boarding time + this, the Saturday is considered gone

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; Egged870AvailabilityMonitor/2.0; read-only, hourly)',
    'Accept': 'application/json',
    'Origin': 'https://reserved-seats.egged.co.il',
    'Referer': 'https://reserved-seats.egged.co.il/',
}

Fetcher = Callable[[str], object]


def get_json(url: str) -> object:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        ctype = r.headers.get('Content-Type', '')
        if 'json' not in ctype:
            raise RuntimeError(f'Non-JSON response ({ctype or "no content-type"}): bot-check or outage?')
        return json.load(r)


def dates_url() -> str:
    q = {'yeshuvIdUp': YESHUV_UP, 'motStopIdUp': STOP_UP, 'yeshuvIdDown': YESHUV_DOWN,
         'motStopIdDown': STOP_DOWN, 'passengerCnt': PASSENGERS, 'rideTypeId': 0}
    return f'{API}/date?{urllib.parse.urlencode(q)}'


def rides_url(travel_date_epoch: int) -> str:
    q = {'travelDate': travel_date_epoch, 'motStopIdUp': STOP_UP, 'motStopIdDown': STOP_DOWN,
         'passengerCnt': PASSENGERS, 'rideTypeId': 0}
    return f'{API}/ride/{YESHUV_UP}/{YESHUV_DOWN}?{urllib.parse.urlencode(q)}'


# ---------- dates ----------

def next_saturday(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=(5 - today.weekday()) % 7)


def parse_hhmm(s: str) -> dt.time:
    return dt.datetime.strptime(s, '%H:%M').time()


def upcoming_saturdays(now: dt.datetime, count: int = 2, target: str = DEFAULT_TIME) -> list[dt.date]:
    """The next `count` Saturdays whose target ride has not departed yet (Israel local time)."""
    local = now.astimezone(TZ)
    first = next_saturday(local.date())
    cutoff = dt.datetime.combine(first, parse_hhmm(target), TZ) + dt.timedelta(minutes=DEPARTED_GRACE_MIN)
    if local >= cutoff:
        first += dt.timedelta(days=7)
    return [first + dt.timedelta(days=7 * i) for i in range(count)]


def utc_to_local(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(TZ)


# ---------- decision ----------

def find_ride(rides: list, day: dt.date, target: dt.time) -> dict | None:
    """Closest ride of LINE between our two stops boarding on `day` within TOLERANCE_MIN of `target`."""
    want = target.hour * 60 + target.minute
    best, best_diff = None, None
    for r in rides:
        if not isinstance(r, dict):
            continue
        if str(r.get('lineSign')) != LINE:
            continue
        if str(r.get('motStopIdUp')) != str(STOP_UP) or str(r.get('motStopIdDown')) != str(STOP_DOWN):
            continue
        iso = r.get('stopUpDt')
        if not isinstance(iso, str):
            continue
        when = utc_to_local(iso)
        if when.date() != day:
            continue
        diff = abs(when.hour * 60 + when.minute - want)
        if diff <= TOLERANCE_MIN and (best_diff is None or diff < best_diff):
            best, best_diff = r, diff
    return best


def check_trip(day: dt.date, target: str = DEFAULT_TIME, *, now: dt.datetime | None = None,
               fetch: Fetcher = get_json, dates: object = None) -> dict:
    """Check one trip. `dates` may be passed in to reuse one /date response for several days."""
    now = now or dt.datetime.now(TZ)
    res = {'date': day.isoformat(), 'requested_time': target, 'line': LINE,
           'stop_up': STOP_UP, 'stop_down': STOP_DOWN, 'status': 'UNKNOWN', 'reason': ''}
    try:
        t = parse_hhmm(target)
        if dates is None:
            dates = fetch(dates_url())
        if not isinstance(dates, list) or not dates:
            res['reason'] = 'Open-dates list is empty or malformed; cannot tell closed from broken'
            return res
        day_str = day.strftime('%d/%m/%Y')
        if not all(isinstance(d, dict) and 'travelDateStr' in d for d in dates):
            res['reason'] = 'Open-dates list has an unexpected shape'
            return res
        entry = next((d for d in dates if d['travelDateStr'] == day_str), None)
        if entry is None:
            offered = f"{dates[0]['travelDateStr']}..{dates[-1]['travelDateStr']}"
            res.update(status='NOT_OPEN', reason=f'Date not offered for booking (site offers {len(dates)} dates, {offered})')
            return res

        rides = fetch(rides_url(int(entry['travelDateEpoch'])))
        if not isinstance(rides, list):
            res['reason'] = 'Ride list malformed'
            return res
        res['rides_on_date'] = len(rides)
        if not rides:
            res['reason'] = 'Date is offered but ride list is empty (inconsistent answer)'
            return res
        ride = find_ride(rides, day, t)
        if ride is None:
            times = ', '.join(sorted({str(r.get('stopUpTimeStr')) for r in rides if isinstance(r, dict)}))
            res.update(status='NOT_OPEN', reason=f'No line {LINE} ride boarding near {target}; offered boarding times: {times}')
            return res

        board = utc_to_local(ride['stopUpDt'])
        deadline = dt.datetime.fromtimestamp(int(ride['maxBookingDtEpoch']), TZ)
        res.update(boarding_time=board.strftime('%H:%M'), makat8=ride.get('makat8'),
                   travel_dt_epoch=ride.get('travelDtEpoch'), booking_deadline=deadline.isoformat(timespec='minutes'))
        if deadline <= now:
            res.update(status='NOT_OPEN', reason='Booking deadline for this ride has passed')
            return res
        res.update(status='OPEN', reason='Date is in the open-dates list, ride is offered, booking deadline is in the future '
                                         '(no seat was reserved by this check)')
        return res
    except Exception as exc:  # any technical failure -> UNKNOWN, never NOT_OPEN
        res.update(status='UNKNOWN', reason=f'Check failed: {type(exc).__name__}: {exc}')
        return res


def run(days: list[dt.date], target: str = DEFAULT_TIME, *, now: dt.datetime | None = None,
        fetch: Fetcher = get_json) -> dict:
    now = now or dt.datetime.now(TZ)
    try:
        dates: object = fetch(dates_url())  # one shared request for all days
    except Exception as exc:
        dates = None
        shared_error = f'Check failed: {type(exc).__name__}: {exc}'
    else:
        shared_error = None
    results = []
    for day in days:
        if shared_error:
            results.append({'date': day.isoformat(), 'requested_time': target, 'line': LINE,
                            'status': 'UNKNOWN', 'reason': shared_error})
        else:
            results.append(check_trip(day, target, now=now, fetch=fetch, dates=dates))
    return {'checked_at': now.astimezone(TZ).isoformat(timespec='seconds'), 'site': SITE, 'results': results}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Read-only check whether an Egged 870 ride can be booked now.')
    p.add_argument('--date', action='append', help='YYYY-MM-DD (repeatable). Default: the next two Saturdays.')
    p.add_argument('--time', default=DEFAULT_TIME, help='Local boarding time at Megiddo Jct, HH:MM (default 19:05)')
    p.add_argument('--out', default='probe_results.json', help='Where to write the JSON report')
    a = p.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    now = dt.datetime.now(TZ)
    days = [dt.date.fromisoformat(d) for d in a.date] if a.date else upcoming_saturdays(now, target=a.time)
    report = run(days, a.time, now=now)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    with open(a.out, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return 1 if any(r['status'] == 'UNKNOWN' for r in report['results']) else 0


if __name__ == '__main__':
    sys.exit(main())
