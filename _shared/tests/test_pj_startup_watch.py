"""Startup-only recovery with isolated exchange logs and a simulated cmux surface."""
import argparse
import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'pj-cmux.py'
CODEX_IDLE = ('› Ask Codex to do anything\n'
              '  configured model · ~/project/backend   ⚠ 3 warnings · f2 to view\n')
GROK_IDLE = ('  ╭────────────────────────────────────────────────────────────╮\n'
             '  │ ❯                                                          │\n'
             '  ╰─────────────────────── Grok 4.7 (high) · always-approve ─╯\n'
             '  Shift+Tab:mode  │  Ctrl+.:shortcuts\n')


@pytest.fixture
def t(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('pj_watch_test', SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, 'TASKS_DIR', tmp_path / 'raw/tasks')
    monkeypatch.setattr(m, 'LOCK_ROOT', str(tmp_path / 'locks'))
    profiles = m.launcher_profiles()
    profiles.update({'work-codex': {'runtime': 'codex', 'argv': ['codex'],
                                  'env': {'CODEX_HOME': str(tmp_path / 'work account')}},
                     'work-low': {'runtime': 'codex', 'argv': ['codex'],
                                  'env': {'CODEX_HOME': str(tmp_path / 'work account')}}})
    monkeypatch.setattr(m, 'launcher_profiles', lambda: profiles)
    monkeypatch.setattr(m, 'TEST', True)
    monkeypatch.setattr(m, 'task_info', lambda slug: {'project': 'p', 'status': '진행 중'})
    m._validate_target_for_test = m.validate_target
    monkeypatch.setattr(m, 'validate_target', lambda *args: None)
    monkeypatch.setattr(m, 'should_relay_inline', lambda: False)
    return m


def request(t, slug='task', role='code'):
    req = t.make_event('review_requested', 'worker', 'reviewer', slug,
                       {'role': role, 'launcher': 'codex', 'requester':
                        {'workspace': 'ws', 'surface': 'requester', 'cwd': '/work/task'},
                        'diff_base': 'base', 'diff_head': 'task', 'summary': 's', 'plan_path': '/plan'},
                       round_=1, request_id=t.new_id('req'))
    t.append_event(t.stream_path('p', slug, 'worker'), req)
    t.watcher('register', 'p', req)
    t.watcher('bind', 'p', req, 'ws', slug + '-surface', 'codex')
    t.watcher('record', 'p', req, 'launched', registered_at=100, sent_at=100,
              trusted_roots=['/work/task'])
    return req


def job(t, req):
    return next(j for j in t.watcher('jobs', 'p') if j['request']['event_id'] == req['event_id'])


def screen(t, monkeypatch, content):
    calls = []
    def cmux(argv, check=True, timeout=None):
        calls.append(argv)
        return content if argv[1] == 'read-screen' else ''
    monkeypatch.setattr(t, 'run_cmux', cmux)
    return calls


def start(t, req, **changes):
    args = dict(action='review.started', slug=req['slug'], role=req['payload']['role'],
                reply_to=req['event_id'], round=1)
    args.update(changes)
    return t.op_request(argparse.Namespace(**args))


@pytest.mark.parametrize('board,expected', [('claude', 'claude'),
                                         ('codex', 'codex'),
                                         ('work-low', 'work-low'), ('grok', 'grok')])
def test_board_watcher_tab_launcher_placement_and_reuse(t, monkeypatch, board, expected):
    monkeypatch.setattr(t, 'resolve_role_target', lambda *a: ('board-ws', 'board-surface', None))
    monkeypatch.setattr(t, 'registry_find_surface', lambda *a: ('board-ws', {'cwd': '/work/board', 'codex_home': '/account/default'}))
    surfaces = [('board-surface', 'board')]
    monkeypatch.setattr(t, 'cmux_tree', lambda *a: ([], {'board-pane': surfaces, 'other-pane': []}))
    req = t.build_watcher_start(argparse.Namespace(board=board), {}, 'p', {})
    role, result = t.deliver_watcher_start('p', None, req)
    assert role == 'board' and result['launcher'] == expected and not result['reused']
    created = [c for c in t.EXECUTED if c[:2] == ['cmux', 'new-surface']]
    assert len(created) == 1
    assert created[0][created[0].index('--pane') + 1] == 'board-pane'
    assert created[0][created[0].index('--workspace') + 1] == 'board-ws'
    assert created[0][created[0].index('--focus') + 1] == 'false'
    assert expected in created[0][created[0].index('--command') + 1]
    assert any(c[:2] == ['cmux', 'rename-tab'] and c[-1] == 'watcher' for c in t.EXECUTED)
    surfaces.append((result['surface'], 'watcher'))
    _, again = t.deliver_watcher_start('p', None, req)
    assert again['reused']
    assert len([c for c in t.EXECUTED if c[:2] == ['cmux', 'new-surface']]) == 1


def test_board_launcher_record_wins_over_worker_account(t):
    opened = t.make_event('workspace_board', 'board', 'board', None,
                          {'launchers': {'board': 'work-codex'}})
    completed = t.make_event('action_completed', 'board', 'board', None,
                             {'workspace': 'board-ws'}, reply_to=opened['event_id'])
    for ev in (opened, completed):
        t.append_event(t.stream_path('p', None, 'board'), ev)
    req = {'payload': {'requester': {'surface': 'worker'}}}
    assert t.watcher('tab_launcher', 'p', 'board-ws', 'board', {'kind': 'codex'}, req) == 'work-codex'
    with pytest.raises(t.Fail):
        t.watcher('tab_launcher', 'p', 'different-ws', 'board', {'kind': 'codex'}, req)


def test_manual_board_profile_comes_from_registry_not_worker_account(t):
    req = {'payload': {'requester': {'surface': 'worker'}}}
    assert t.watcher('tab_launcher', 'p', 'ws', 'board',
                     {'kind': 'codex', 'profile': 'work-codex'}, req) == 'work-codex'
    with pytest.raises(t.Fail):
        t.watcher('tab_launcher', 'p', 'ws', 'board', {'kind': 'codex'}, req)


def test_failed_start_notifies_console_and_requester_once(t, monkeypatch):
    req = request(t)
    attached = t.make_event('watcher_attached', 'board', 'board', None,
                            {'workspace': 'board-ws', 'surface': 'watcher', 'launcher': 'work-codex'})
    t.append_event(t.stream_path('p', None, 'board'), attached)
    calls = screen(t, monkeypatch, '')
    t.watcher('tick', 'p', clock=400)
    messages = [c for c in calls if c[1] == 'send' and c[-1] != r'\r']
    assert len(messages) == 2
    assert messages[0][-1] == messages[1][-1]
    assert 'watcher' in messages[0] and 'requester' in messages[1]
    calls.clear()
    t.watcher('tick', 'p', clock=500)
    assert not calls


def test_dead_console_does_not_block_requester_notification(t, monkeypatch):
    req = request(t)
    t.append_event(t.stream_path('p', None, 'board'),
                   t.make_event('watcher_attached', 'board', 'board', None,
                                {'workspace': 'board-ws', 'surface': 'dead', 'launcher': 'codex'}))
    calls = screen(t, monkeypatch, '')
    def validate(ws, surf, *args):
        if surf == 'dead':
            raise t.Fail(3, 'console gone')
    monkeypatch.setattr(t, 'validate_target', validate)
    t.watcher('tick', 'p', clock=400)
    assert job(t, req)['console_notification_error'] == 'console gone'
    assert any(c[1] == 'send' and 'requester' in c for c in calls)
    assert not any(c[1] == 'send' and 'dead' in c for c in calls)


def test_update_skip_then_resend_same_request_and_stop_after_started(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, 'Update available!\n❯ 1. Update now\n  2. Skip for now\n')
    t.watcher('tick', 'p', clock=105)
    assert [c[-1] for c in calls if c[1] == 'send-key'] == ['down', 'enter']
    assert not any(c[1] == 'send' for c in calls)
    # A repeated identical dialog never gets another Enter.
    t.watcher('tick', 'p', clock=110)
    assert len([c for c in calls if c[1] == 'send-key']) == 2
    calls = screen(t, monkeypatch, '❯\n? for shortcuts')
    t.watcher('tick', 'p', clock=116)
    sent = [c[-1] for c in calls if c[1] == 'send']
    assert req['event_id'] in sent[0] and 'event prompt' in sent[0]
    assert job(t, req)['resends'] == 1
    start(t, req)
    calls.clear()
    t.watcher('tick', 'p', clock=200)
    assert calls == []
    assert job(t, req)['state'] == 'completed'
    assert not any(e['type'] == 'code_review' for e in t.read_stream(t.reviewer_stream('p', 'task', 'code')))


def test_trust_only_exact_registered_worktree(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, '/work/task\nDo you trust this folder?\n❯ 1. Yes, I trust this folder\n  2. No, exit\n')
    t.watcher('tick', 'p', clock=105)
    assert [c[-1] for c in calls if c[1] == 'send-key'] == ['enter']
    other = request(t, 'other')
    calls = screen(t, monkeypatch, '/work/task-evil\nDo you trust this folder?\n❯ 1. Yes, I trust this folder\n  2. No, exit\n')
    t.watcher('tick', 'p', clock=110)
    assert not any(c[1] == 'send-key' for c in calls)
    assert job(t, other)['state'] == 'failed'


def test_unknown_approval_is_not_entered(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, 'Do you want to proceed?\n❯ 1. Yes\n2. No')
    t.watcher('tick', 'p', clock=105)
    assert not any(c[1] == 'send-key' for c in calls)
    assert job(t, req)['state'] == 'failed'


def test_known_welcome_enter_but_not_command_confirmation(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, 'Welcome to Codex\nPress Enter to continue')
    t.watcher('tick', 'p', clock=105)
    assert [c[-1] for c in calls if c[1] == 'send-key'] == ['enter']
    classifier = t._STARTUP_WATCH.classify
    assert classifier('Press Enter to run this command', [])[1] == []
    assert classifier('Welcome to Codex\nPermission required\nPress Enter to continue', [])[1] == []


def test_process_alive_and_busy_is_not_started_or_resubmitted(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, 'Working…\nesc to interrupt\n? for shortcuts')
    t.watcher('tick', 'p', clock=140)
    assert job(t, req)['state'] == 'launched'
    assert not any(c[1] in ('send', 'send-key') for c in calls)


@pytest.mark.parametrize('indicator,expected', [('Working… (esc to interrupt)', 'busy'),
                                             ('Permission required', 'blocked')])
def test_busy_or_approval_wins_over_stale_startup_dialog(t, monkeypatch, indicator, expected):
    req = request(t)
    content = 'Update available!\n❯ 1. Update now\n  2. Skip for now\n' + indicator
    calls = screen(t, monkeypatch, content)
    assert t.watcher('screen_kind', content) == expected
    t.watcher('tick', 'p', clock=120)
    assert not any(c[1] in ('send', 'send-key') and 'task-surface' in c for c in calls)


@pytest.mark.parametrize('launcher', ['codex', 'work-codex', 'work-low'])
def test_reused_codex_reviewer_receives_second_round_without_new_tab(t, monkeypatch, launcher):
    req = request(t)
    req['round'] = 2
    req['payload']['launcher'] = launcher
    monkeypatch.setattr(t, 'latest_reusable_reviewer_attached', lambda *a:
                        {'workspace': 'ws', 'surface': 'task-surface', 'launcher': launcher})
    monkeypatch.setattr(t, 'new_surface_in_pane', mock.Mock(side_effect=AssertionError('must reuse')))
    calls = screen(t, monkeypatch, CODEX_IDLE)
    _, result = t.deliver_code_review('p', 'task', req)
    assert result['reused'] and not result['awaiting_start']
    messages = [c[-1] for c in calls if c[1] == 'send' and c[-1] != r'\r']
    assert len(messages) == 1 and req['event_id'] in messages[0]


def test_watcher_resends_to_idle_codex_and_stops_on_exact_start(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, '\x1b[2m' + CODEX_IDLE + '\x1b[0m')
    t.watcher('tick', 'p', clock=110)
    assert job(t, req)['resends'] == 1
    messages = [c[-1] for c in calls if c[1] == 'send' and c[-1] != r'\r']
    assert len(messages) == 1 and req['event_id'] in messages[0]
    start(t, req)
    calls.clear()
    t.watcher('tick', 'p', clock=120)
    assert calls == [] and job(t, req)['state'] == 'completed'


def test_grok_status_approval_mode_is_idle_not_approval_request(t, monkeypatch):
    req = request(t)
    req['payload']['launcher'] = 'grok'
    t.watcher('record', 'p', req, 'launched', launcher='grok')
    content = 'Update: v1.0.41 available — press ctrl+u to restart\n' + GROK_IDLE
    calls = screen(t, monkeypatch, content)
    # Use real runtime/target validation; only the native process query is simulated.
    monkeypatch.setattr(t, 'validate_target', t._validate_target_for_test)
    native = mock.Mock(return_value=True)
    monkeypatch.setattr(t, 'native_grok_alive', native)
    assert t.watcher('screen_kind', content) == 'ready'
    assert t.send_review_when_ready('p', 'task', req, 'ws', 'task-surface', 'grok')
    native.assert_called_with('ws', 'task-surface')
    messages = [c[-1] for c in calls if c[1] == 'send' and c[-1] != r'\r']
    assert len(messages) == 1 and req['event_id'] in messages[0]
    assert not any(c[1] == 'send-key' for c in calls)
    calls.clear()
    t.watcher('tick', 'p', clock=110)
    assert job(t, req)['resends'] == 1
    assert any(c[1] == 'send' and req['event_id'] in c[-1] for c in calls)
    start(t, req)
    calls.clear()
    t.watcher('tick', 'p', clock=120)
    assert not calls and job(t, req)['state'] == 'completed'


@pytest.mark.parametrize('prefix,expected', [
    ('Approve this command?\n', 'blocked'),
    ('Do you want to proceed?\n', 'blocked'),
    ('Thinking...\nesc to interrupt\n', 'busy'),
    ('Permission required\n', 'blocked'),
])
def test_grok_empty_composer_does_not_override_active_dialog_or_work(t, monkeypatch, prefix, expected):
    req = request(t)
    t.watcher('record', 'p', req, 'launched', launcher='grok')
    content = prefix + GROK_IDLE
    calls = screen(t, monkeypatch, content)
    assert t.watcher('screen_kind', content) == expected
    assert not t.send_review_when_ready('p', 'task', req, 'ws', 'task-surface', 'grok')
    t.watcher('tick', 'p', clock=110)
    assert not any(c[1] in ('send', 'send-key') and 'task-surface' in c for c in calls)


def test_grok_status_label_alone_or_typed_draft_is_not_ready(t):
    assert t.watcher('screen_kind', 'Grok 4.7 (high) · always-approve') != 'ready'
    assert t.watcher('screen_kind', GROK_IDLE.replace('│ ❯ ', '│ ❯ draft ')) != 'ready'
    assert t.watcher('screen_kind', GROK_IDLE + '\n'.join(['other output'] * 9)) != 'ready'


@pytest.mark.parametrize('prefix,expected', [
    ('Working… (esc to interrupt)\n', 'busy'),
    ('esc to stop\n', 'busy'),
    ('Would you like to run the following command?\n', 'blocked'),
    ('Permission required\n', 'blocked'),
])
def test_codex_placeholder_does_not_override_busy_or_approval(t, monkeypatch, prefix, expected):
    req = request(t)
    calls = screen(t, monkeypatch, prefix + CODEX_IDLE)
    assert t.watcher('screen_kind', prefix + CODEX_IDLE) == expected
    assert not t.send_review_when_ready('p', 'task', req, 'ws', 'task-surface', 'work-codex')
    t.watcher('tick', 'p', clock=110)
    # A blocked-screen failure can notify the requester, but never type into the reviewer.
    assert not any(c[1] in ('send', 'send-key') and 'task-surface' in c for c in calls)


@pytest.mark.parametrize('text', [
    'The phrase "ask codex to do anything" appears in the log.',
    CODEX_IDLE + '\n'.join(['other output'] * 7),
    '› user has already typed a draft\nconfigured model',
    'grok> unknown input state',
])
def test_codex_ready_requires_empty_composer_at_bottom(t, text):
    assert t.watcher('screen_kind', text) == 'unknown'


def test_problem_notification_contains_diagnostic_evidence(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, 'unrecognized screen')
    t.watcher('tick', 'p', clock=110)
    t.watcher('tick', 'p', clock=400)
    rows = t.read_stream(t.stream_path('p', 'task', 'worker'))
    failure = next(e for e in rows if e['type'] == 'review_start_failed')
    assert failure['payload']['observed'] == 'unknown'
    assert failure['payload']['last_screen'] == 'unrecognized screen'
    assert failure['payload']['resends'] == 0


def test_start_claim_exact_request_role_and_idempotency(t, capsys):
    req = request(t)
    with pytest.raises(t.Fail):
        start(t, req, role='security')
    start(t, req)
    assert 'PJ_REVIEW_STARTED=started' in capsys.readouterr().out
    start(t, req)
    assert 'PJ_REVIEW_STARTED=duplicate' in capsys.readouterr().out
    rows = t.read_stream(t.reviewer_stream('p', 'task', 'code'))
    assert len([e for e in rows if e['type'] == 'review_started']) == 1
    newer = request(t)
    with pytest.raises(t.Fail):
        start(t, req)
    start(t, newer)


def test_old_scope_cancelled_and_other_task_still_polled(t, monkeypatch):
    old = request(t)
    newer = request(t)
    other = request(t, 'other')
    calls = screen(t, monkeypatch, '❯\n? for shortcuts')
    t.watcher('tick', 'p', clock=111)
    assert job(t, old)['state'] == 'cancelled'
    assert job(t, newer)['resends'] == 1
    assert job(t, other)['resends'] == 1


def test_missing_tab_timeout_is_visible_without_blocking_other_task(t, monkeypatch):
    absent = request(t, 'absent')
    t.watcher('record', 'p', absent, 'registered', surface=None, registered_at=0)
    active = request(t, 'active')
    calls = screen(t, monkeypatch, '❯\n? for shortcuts')
    t.watcher('tick', 'p', clock=181)
    assert job(t, absent)['state'] == 'failed'
    assert job(t, active)['state'] == 'resent'
    assert t.fold_status(t.load_events('p', 'absent'), absent)['state'] == 'failed'
    assert any('requester' in c and c[1] == 'send' for c in calls)


def test_no_prompt_sent_to_shell_after_agent_exit(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, 'Welcome to Codex\nPress Enter to continue')
    monkeypatch.setattr(t, 'validate_target', mock.Mock(side_effect=t.Fail(3, 'agent gone')))
    monkeypatch.setattr(t, 'cmux_tree', lambda ws: ([], {}))
    t.watcher('tick', 'p', clock=120)
    assert job(t, req)['state'] == 'failed'
    assert not any(c[1] in ('send', 'send-key') for c in calls)


def test_retries_are_bounded(t, monkeypatch, capsys):
    req = request(t)
    calls = screen(t, monkeypatch, '❯\n? for shortcuts')
    for clock in (111, 122, 133):
        t.watcher('tick', 'p', clock=clock)
    assert job(t, req)['resends'] == 2
    assert job(t, req)['state'] == 'failed'
    rows = t.read_stream(t.stream_path('p', 'task', 'worker'))
    failed = [e for e in rows if e['type'] == 'review_start_failed']
    assert len(failed) == 1
    assert '자동 재요청을 중단' in failed[0]['payload']['recovery']
    calls.clear()
    t.watcher('tick', 'p', clock=400)
    assert calls == []
    # Transport retry must not start another delivery or reset the failed watch.
    monkeypatch.setattr(t, 'find_request', lambda *_: ('p', None, req, t.stream_path('p', 'task', 'worker')))
    monkeypatch.setattr(t, 'find_terminal', lambda *_: {'type': 'delivered'})
    t.op_retry(argparse.Namespace(request_id=req['request_id'], slug='task'))
    output = capsys.readouterr().out
    assert 'already-delivered' in output and '새 review.request로 우회하지 마세요' in output
    assert 'PJ_CMUX_REQUEST=' not in output
    assert t.read_stream(t.stream_path('p', 'task', 'worker')) == rows


def test_restart_resumes_existing_request_state(t, monkeypatch):
    req = request(t)
    calls = screen(t, monkeypatch, '❯\n? for shortcuts')
    t.watcher('tick', 'p', clock=111)
    t._STARTUP_WATCH = None
    t.watcher('tick', 'p', clock=122)
    assert job(t, req)['resends'] == 2
    assert len([e for e in t.read_stream(t.stream_path('p', 'task', 'worker')) if e['type'] == 'review_requested']) == 1


def test_production_start_claim_rejects_wrong_surface(t, monkeypatch):
    req = request(t)
    monkeypatch.setattr(t, 'TEST', False)
    monkeypatch.setenv('CMUX_SURFACE_ID', 'not-the-reviewer')
    monkeypatch.setenv('CMUX_WORKSPACE_ID', 'ws')
    with pytest.raises(t.Fail):
        start(t, req)
    monkeypatch.setenv('CMUX_SURFACE_ID', 'task-surface')
    start(t, req)


def test_project_daemon_is_singleton_and_stops_in_scratch_vault(tmp_path):
    env = dict(os.environ, PJ_VAULT=str(tmp_path), PJ_CMUX_TEST='1')
    cmd = [sys.executable, str(SCRIPT), 'watcher', 'run', '--project', 'scratch']
    first = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        board = tmp_path / 'raw/tasks/scratch/exchanges/board.jsonl'
        deadline = time.monotonic() + 5
        while not board.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert board.exists()
        second = subprocess.run(cmd, env=env, capture_output=True, timeout=5)
        assert second.returncode == 0
        assert first.poll() is None
        stopped = subprocess.run([sys.executable, str(SCRIPT), 'watcher', 'stop', '--project', 'scratch'],
                                 env=env, capture_output=True, timeout=5)
        assert stopped.returncode == 0, stopped.stderr
        status = subprocess.run([sys.executable, str(SCRIPT), 'watcher', 'status', '--project', 'scratch'],
                                env=env, capture_output=True, text=True, timeout=5, check=True)
        assert json.loads(status.stdout)['interval_seconds'] == 10
        assert first.wait(timeout=13) == 0
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)


def test_registration_precedes_tab_creation_and_binding_precedes_launch(t, monkeypatch):
    monkeypatch.setenv('CMUX_WORKSPACE_ID', 'ws')
    monkeypatch.setenv('CMUX_SURFACE_ID', 'requester')
    group = t.make_event('review_group_opened', 'worker', 'worker', 'task',
                         {'summary': 's', 'diff_base': 'base', 'diff_head': 'task', 'roles': ['code']}, round_=1)
    t.append_event(t.stream_path('p', 'task', 'worker'), group)
    args = argparse.Namespace(action='review.request', slug='task', project=None, stdin=False,
                              role='code', round=1, launcher=None, findings=None)
    t.op_request(args)
    req = next(e for e in t.read_stream(t.stream_path('p', 'task', 'worker')) if e['type'] == 'review_requested')
    assert job(t, req)['state'] == 'registered'
    def create(*args):
        assert job(t, req)['state'] == 'registered'
        return 'reviewer-surface'
    def boot(*args):
        assert job(t, req)['state'] == 'attached'
        assert job(t, req)['surface'] == 'reviewer-surface'
        return False  # CLI is at a startup dialog, so the initial message is not delivered.
    monkeypatch.setattr(t, 'new_surface_in_pane', create)
    monkeypatch.setattr(t, 'boot_agent', boot)
    t.relay_request(req['request_id'], 'task')
    assert t.fold_status(t.load_events('p', 'task'), req)['state'] == 'starting'
    assert job(t, req)['state'] == 'launched'
    # Retry of the same queued request does not create a second reviewer.
    monkeypatch.setattr(t, 'new_surface_in_pane', mock.Mock(side_effect=AssertionError('duplicate tab')))
    t.relay_request(req['request_id'], 'task')


def test_real_request_path_queues_a_blocked_existing_reviewer(t, monkeypatch):
    req = request(t)
    monkeypatch.setattr(t, 'latest_reusable_reviewer_attached', lambda *a:
                        {'workspace': 'ws', 'surface': 'task-surface', 'launcher': 'codex'})
    calls = screen(t, monkeypatch, 'Update available!\n❯ 1. Update now\n  2. Skip')
    # Same actual readiness path as production; no TEST-only delivery shortcut.
    original = t.run_cmux
    monkeypatch.setattr(t, 'run_cmux', lambda argv, check=True, timeout=None: original(argv, check))
    _, result = t.deliver_code_review('p', 'task', req)
    assert result['awaiting_start'] is True
    assert not any(c[1] == 'send' for c in calls)


def test_planner_prompt_claims_start_for_the_exact_event(t):
    req = request(t, role='plan')
    prompt = t.reviewer_prompt_for('p', 'task', req)
    assert 'request review.started --slug task --role plan' in prompt
    assert req['event_id'] in prompt
    assert 'duplicate' in prompt
    assert 'pj-plan/SKILL.md' in prompt


def test_planner_prompt_quotes_relocated_package_paths(t, monkeypatch, tmp_path):
    t.watcher('jobs', 'p')
    scripts = tmp_path / "package ' with spaces" / '_shared/scripts'
    monkeypatch.setattr(t, 'SCRIPT_DIR', scripts)
    req = request(t, role='plan')
    prompt = t.reviewer_prompt_for('p', 'task', req)
    assert shlex.quote(str(scripts / 'pj-cmux.py')) in prompt
    assert shlex.quote(str(scripts.parent.parent / 'pj-plan/SKILL.md')) in prompt


def test_manual_board_captures_account_before_relay_and_ignores_worker_context(t, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', "/account ' with spaces")
    req = t.build_watcher_start(argparse.Namespace(board='codex'), {}, 'p', {})
    req['payload']['requester'] = {'workspace': 'board-ws', 'surface': 'board-surface', 'cwd': '/work/board'}
    monkeypatch.setenv('CODEX_HOME', '/wrong/relay/account')
    monkeypatch.setattr(t, 'resolve_role_target', lambda *a: ('board-ws', 'board-surface', None))
    monkeypatch.setattr(t, 'registry_find_surface', lambda *a: ('board-ws', {'kind': 'codex'}))
    monkeypatch.setattr(t, 'cmux_tree', lambda *a: ([], {'pane': [('board-surface', 'board')]}))
    t.deliver_watcher_start('p', None, req)
    command = next(c[c.index('--command') + 1] for c in t.EXECUTED if c[:2] == ['cmux', 'new-surface'])
    assert 'export CODEX_HOME=' + shlex.quote("/account ' with spaces") + ';' in command
    # In another project, a worker cannot donate its cwd/account as board context.
    req['payload']['requester']['surface'] = 'worker-surface'
    with pytest.raises(t.Fail):
        t.deliver_watcher_start('other', None, req)


def test_native_process_detection_uses_declared_runtime_not_profile_prefix(t, monkeypatch):
    surface = 'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA'
    monkeypatch.setattr(t, 'validate_target', mock.Mock(side_effect=t.Fail(3, 'not registered yet')))
    monkeypatch.setattr(t, 'cmux_tree', lambda *a: ([], {'pane': [(surface, 'reviewer')]}))
    def cmux(argv, **kwargs):
        if argv[1] == 'tree':
            return f'surface surface:7 {surface} [terminal] "reviewer"'
        return 'a\tb\tc\tprocess\te\tsurface:7\t/usr/bin/codex\n'
    monkeypatch.setattr(t, 'run_cmux', cmux)
    assert t.watcher('alive', {'workspace': 'ws', 'surface': surface, 'launcher': 'work-codex'})
    assert not t.watcher('alive', {'workspace': 'ws', 'surface': surface, 'launcher': 'claude'})


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_watcher_locks_reject_linked_or_special_files(t, tmp_path, kind):
    root = Path(t.LOCK_ROOT)
    root.mkdir()
    target = tmp_path / 'outside'
    target.write_text('keep')
    path = root / 'request-test.lock'
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, path)
    else:
        os.mkfifo(path)
    with pytest.raises((t.Fail, OSError)):
        with t.request_lock('request-test'):
            pytest.fail('unsafe lock accepted')
    with pytest.raises((t.Fail, OSError)):
        with t.watcher('try_lock', 'request-test'):
            pytest.fail('unsafe watcher lock accepted')
    assert target.read_text() == 'keep'


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_watcher_log_rejects_linked_or_special_files(t, monkeypatch, tmp_path, kind):
    t.watcher('jobs', 'p')  # Load the helper without starting any monitor.
    monkeypatch.setattr(t, 'TEST', False)
    monkeypatch.setattr(t._STARTUP_WATCH, 'running', lambda *a: False)
    spawn = mock.Mock(side_effect=AssertionError('must not spawn'))
    monkeypatch.setattr(t._STARTUP_WATCH.subprocess, 'Popen', spawn)
    path = t.exchanges_dir('p') / 'watcher.log'
    path.parent.mkdir(parents=True)
    target = tmp_path / 'outside'
    target.write_text('keep')
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, path)
    else:
        os.mkfifo(path)
    with pytest.raises((t.Fail, OSError)):
        t.watcher('ensure', 'p')
    spawn.assert_not_called()
    assert target.read_text() == 'keep'


@pytest.mark.parametrize('mode', ['start', 'run', 'tick'])
def test_sandbox_cannot_start_a_direct_cmux_controller(t, monkeypatch, mode):
    monkeypatch.setattr(t, 'TEST', False)
    monkeypatch.setenv('CODEX_SANDBOX', 'test-sandbox')
    with pytest.raises(t.Fail, match='sandbox'):
        t.op_watcher(argparse.Namespace(mode=mode, project='p'))


def test_external_hook_thread_identity_does_not_imply_sandbox(t, monkeypatch):
    t.watcher('jobs', 'p')
    monkeypatch.setattr(t, 'TEST', False)
    monkeypatch.delenv('CODEX_SANDBOX', raising=False)
    monkeypatch.setenv('CODEX_THREAD_ID', 'test-thread')
    run = mock.Mock(return_value=0)
    monkeypatch.setattr(t._STARTUP_WATCH, 'run', run)
    assert t.op_watcher(argparse.Namespace(mode='run', project='p')) == 0
    run.assert_called_once()
