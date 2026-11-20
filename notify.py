"""Send one alert per OPEN trip date. Separate from monitor.py so checks can run without alerts.

Dedup state lives in GitHub Issues titled "[EGGED 870 OPEN] YYYY-MM-DD" (open or closed), which
persist between GitHub Actions runs. Telegram is sent first, the Issue is recorded second: if
recording fails the job fails loudly (a rare duplicate is preferable to a silently missed alert).

  python notify.py --dry-run     # print what would be sent, contact nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

TITLE = '[EGGED 870 OPEN] {date}'
SITE = 'https://reserved-seats.egged.co.il/reserved-seats-app/order'


def api_request(url, token, method='GET', payload=None):
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'egged-monitor'}
    data = json.dumps(payload).encode() if payload is not None else None
    if data:
        headers['Content-Type'] = 'application/json'
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=20) as r:
        return json.load(r)


def existing_titles(base, token):
    titles, page = set(), 1
    while True:
        batch = api_request(f'{base}?state=all&per_page=100&page={page}', token)
        titles |= {i.get('title') for i in batch if 'pull_request' not in i}
        if len(batch) < 100:
            return titles
        page += 1


def telegram_send(token, chat_id, message):
    uri = f'https://api.telegram.org/bot{token}/sendMessage'
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': message, 'disable_web_page_preview': 'true'}).encode()
    with urllib.request.urlopen(urllib.request.Request(uri, data=data, method='POST'), timeout=20) as r:
        if json.load(r).get('ok') is not True:
            raise RuntimeError('Telegram rejected notification')


def build_message(r):
    return (f'🚌 קו 870 פתוח להזמנה!\n'
            f'מוצאי שבת {r["date"]}, צומת מגידו → סורוקה/אונ\' בן גוריון, עלייה {r.get("boarding_time") or r.get("requested_time")}.\n'
            f'לא הוזמן מקום – יש להיכנס ולהזמין בעצמך:\n{SITE}')


def open_candidates(report, today):
    return [r for r in report.get('results', [])
            if r.get('status') == 'OPEN' and dt.date.fromisoformat(r['date']) >= today]


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--report', default='probe_results.json')
    p.add_argument('--dry-run', action='store_true', help='Print alerts only; no Telegram, no GitHub')
    p.add_argument('--test-telegram', action='store_true', help='Send one test message; ignores the report and dedup')
    a = p.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')  # Windows consoles default to cp1255
    if a.test_telegram:
        tg_token, chat_id = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
        if not (tg_token and chat_id):
            sys.exit('TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets are missing')
        telegram_send(tg_token, chat_id, '✅ הודעת ניסיון ממנטר קו 870. זו לא התראת זמינות.')
        print('Test Telegram message sent.')
        return
    with open(a.report, encoding='utf-8') as fh:
        report = json.load(fh)
    from zoneinfo import ZoneInfo
    today = dt.datetime.now(ZoneInfo('Asia/Jerusalem')).date()
    candidates = open_candidates(report, today)
    if not candidates:
        print('No OPEN dates; no notification.')
        return
    if a.dry_run:
        for r in candidates:
            print(f'[dry-run] would alert for {r["date"]}:\n{build_message(r)}')
        return

    base = f'https://api.github.com/repos/{os.environ["GITHUB_REPOSITORY"]}/issues'
    gh_token = os.environ['GH_TOKEN']
    known = existing_titles(base, gh_token)
    tg_token, chat_id = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    for r in candidates:
        title = TITLE.format(date=r['date'])
        if title in known:
            print(f'Already notified for {r["date"]}; skipping.')
            continue
        message = build_message(r)
        if tg_token and chat_id:
            telegram_send(tg_token, chat_id, message)
            print(f'Telegram notification sent for {r["date"]}')
        else:
            print(f'Telegram not configured; the GitHub Issue is the only alert for {r["date"]}')
        api_request(base, gh_token, method='POST', payload={'title': title, 'body': message})
        known.add(title)
        print(f'Recorded {title}; future alerts for this date suppressed.')


if __name__ == '__main__':
    main()
