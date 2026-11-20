"""Failure alerts: tell the user when the monitor itself stops working.

A run "fails" if probe_results.json is missing/unreadable or any result is UNKNOWN.
State lives in one open GitHub Issue titled "[EGGED 870 HEALTH]" whose body holds JSON:
  {"consecutive_failures": N, "first_failure": "...", "last_alert_date": "YYYY-MM-DD" | null, "last_reason": "..."}

- After FAIL_THRESHOLD consecutive failing runs -> one Telegram alert, at most one per (Israel) day.
- On the first healthy run -> the Issue is closed; a "recovered" message is sent only if an alert was sent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import notify

TZ = ZoneInfo('Asia/Jerusalem')
TITLE = '[EGGED 870 HEALTH]'
FAIL_THRESHOLD = 3
RUNS_URL_HINT = 'https://github.com/{repo}/actions'


def run_failure(report_path: str) -> str | None:
    """None if healthy, else a short reason."""
    try:
        with open(report_path, encoding='utf-8') as fh:
            report = json.load(fh)
        results = report['results']
        if not results:
            return 'probe report has no results'
    except Exception as exc:
        return f'probe report missing or unreadable ({type(exc).__name__})'
    bad = [r for r in results if r.get('status') == 'UNKNOWN']
    if bad:
        return '; '.join(f"{r.get('date')}: {r.get('reason', '')}"[:200] for r in bad)
    return None


def parse_state(body: str | None) -> dict:
    try:
        start, end = body.index('{'), body.rindex('}') + 1
        return json.loads(body[start:end])
    except Exception:
        return {}


def render_state(state: dict) -> str:
    return ('Monitor health state (managed automatically; close only if you know the monitor works).\n\n'
            '```json\n' + json.dumps(state, ensure_ascii=False, indent=2) + '\n```')


def find_open_issue(base, token):
    page = 1
    while True:
        batch = notify.api_request(f'{base}?state=open&per_page=100&page={page}', token)
        for i in batch:
            if i.get('title') == TITLE and 'pull_request' not in i:
                return i
        if len(batch) < 100:
            return None
        page += 1


def step(failure: str | None, issue: dict | None, now: dt.datetime) -> tuple[str, dict | None, str | None]:
    """Pure decision. Returns (action, new_state, telegram_message).
    action: 'none' | 'create' | 'update' | 'close'."""
    today = now.astimezone(TZ).date().isoformat()
    if failure is None:
        if issue is None:
            return 'none', None, None
        state = parse_state(issue.get('body'))
        msg = None
        if state.get('last_alert_date'):
            msg = '✅ מנטר קו 870 חזר לפעול. הבדיקה השעתית מצליחה שוב.'
        return 'close', state, msg
    state = parse_state(issue.get('body')) if issue else {}
    state['consecutive_failures'] = int(state.get('consecutive_failures', 0)) + 1
    state.setdefault('first_failure', now.astimezone(TZ).isoformat(timespec='minutes'))
    state.setdefault('last_alert_date', None)
    state['last_reason'] = failure
    msg = None
    if state['consecutive_failures'] >= FAIL_THRESHOLD and state['last_alert_date'] != today:
        state['last_alert_date'] = today
        msg = (f'⚠️ תקלה במנטר קו 870: {state["consecutive_failures"]} בדיקות ברציפות נכשלו '
               f'(מאז {state["first_failure"]}).\n'
               f'בזמן התקלה לא יישלחו התראות זמינות. כדאי לבדוק את האתר ידנית.\n'
               f'סיבה אחרונה: {failure[:300]}')
    return ('update' if issue else 'create'), state, msg


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--report', default='probe_results.json')
    a = p.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    repo = os.environ['GITHUB_REPOSITORY']
    token = os.environ['GH_TOKEN']
    base = f'https://api.github.com/repos/{repo}/issues'
    failure = run_failure(a.report)
    issue = find_open_issue(base, token)
    action, state, msg = step(failure, issue, dt.datetime.now(TZ))
    print(f'health: failure={failure!r} action={action} state={state}')
    if msg:
        msg += f'\n{RUNS_URL_HINT.format(repo=repo)}'
        tg_token, chat_id = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
        if tg_token and chat_id:
            notify.telegram_send(tg_token, chat_id, msg)
            print('health: Telegram message sent')
        else:
            print('health: Telegram not configured; message not sent')
    if action == 'create':
        notify.api_request(base, token, method='POST', payload={'title': TITLE, 'body': render_state(state)})
    elif action == 'update':
        notify.api_request(f"{base}/{issue['number']}", token, method='PATCH', payload={'body': render_state(state)})
    elif action == 'close':
        notify.api_request(f"{base}/{issue['number']}", token, method='PATCH', payload={'state': 'closed'})


if __name__ == '__main__':
    main()
