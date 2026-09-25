"""Night CLI + real task registry/event streams/git; cmux transport is stubbed."""
import importlib.util
import json
import pathlib
import os
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('flow', ROOT / '_shared/tests/test_e2e_multirepo_flow.py')
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)
world = flow.world
board = flow.board
agents = flow.agents
NQ = ROOT / 'pj-night/scripts/night-queue.py'


def test_night_cli_dependency_merge_and_report(world, board, agents):
    # Select the synchronous shell transport assumed by these fixtures.
    world['env'].pop('CODEX_SANDBOX', None)
    world['env'].pop('CODEX_THREAD_ID', None)
    def cmd(script, *args, expect=0, cwd=None):
        return flow.sh(script, *args, env=world['env'], cwd=cwd or board['cwd'],
                       expect=expect).stdout
    def night(op, *args):
        return cmd(NQ, op, '--project', 'ctx', *args)
    for slug, deps in [('night-a', []), ('night-b', ['--deps', 'night-a'])]:
        cmd(flow.TASKS, 'add', '--project', 'ctx', '--slug', slug, '--title', slug,
            '--repos', 'example-frontend', *deps)
    assert 'count=2' in night('plan', '--parallel', '2')
    main_before = flow.git(world['repos']['example-frontend'], 'rev-parse', 'main')
    for slug in ['night-a', 'night-b']:
        assert f'PJ_NIGHT=next slug={slug}' in night('next')
        if slug == 'night-a':
            assert 'held=night-b' in night('next')
        created = flow.start_task(world, slug, board['cwd'])
        wt = created[0]['WORKTREE_ABS']
        flow.commit_work(wt, f'{slug}.txt')
        planner = flow.as_role(world, flow.TEST_WS, flow.PLANNER_SURF)
        worker = flow.as_role(world, flow.TEST_WS, flow.WORKER_SURF)
        reviewer = flow.as_role(world, flow.TEST_WS, flow.REVIEWER_SURF)
        out, _ = flow.request(world, worker, wt, 'done.report', '--slug', slug,
                              '--commit', 'ok', expect=3)
        assert 'night 검사 거부' in out
        flow.request(world, planner, wt, 'worker.handoff', '--slug', slug,
                     stdin=json.dumps({'cautions': [], 'references': [], 'reviewers': ['grok']}))
        flow.request(world, worker, wt, 'review.group.open', '--slug', slug,
                     stdin=json.dumps({'summary': 'night fixture review',
                                       'diff_base': 'feat/ctx-fe', 'diff_head': f'feat/{slug}'}))
        for role in ('code', 'plan', 'grok'):
            flow.request(world, worker, wt, 'review.request', '--slug', slug, '--role', role)
            _, rid = flow.request(world, planner if role == 'plan' else reviewer, wt,
                                  'review.reply', '--slug', slug, '--role', role,
                                  stdin=json.dumps({'findings': []}))
            flow.ack(world, worker, wt, slug, rid)
        flow.request(world, worker, wt, 'review.complete', '--slug', slug)
        cmd(flow.CMUX, 'request', 'done.report', '--slug', slug, '--commit', 'ok', cwd=wt)
        assert f'PJ_NIGHT=report slug={slug}' in night('watch', '--quiet', '0', '--timeout', '1')
        assert f'PJ_NIGHT=ready slug={slug}' in night('check', '--slug', slug)
        cmd(flow.TASKS, 'review', '--slug', slug)
        flow.squash_merge(board['fe']['WORKTREE_ABS'], f'feat/{slug}', slug)
        cmd(flow.TASKS, 'done', '--slug', slug, '--merged', 'example-frontend')
        night('record', '--slug', slug, '--outcome', 'merged')
    assert 'PJ_NIGHT=done' in night('next')
    assert '머지됨 (2)' in night('report')
    state = json.loads((world['tasks'] / 'ctx/night.json').read_text())
    assert state['state'] == 'finished' and not state['inflight']
    assert flow.git(world['repos']['example-frontend'], 'rev-parse', 'main') == main_before
    for slug in ['night-a', 'night-b']:
        assert (pathlib.Path(board['fe']['WORKTREE_ABS']) / f'{slug}.txt').is_file()


@pytest.fixture
def queue_cli(tmp_path):
    vault = tmp_path / 'vault'
    env = dict(os.environ, PJ_VAULT=str(vault), PYTHONDONTWRITEBYTECODE='1')

    def call(script, *args):
        return flow.sh(sys.executable, script, *args, env=env).stdout

    call(flow.TASKS, 'proj-reg', '--project', 'night-test', '--repo', 'demo-repo',
         '--branch', 'feat/night-test', '--surface', 'TEST-SURFACE', '--title', 'Night test')
    for slug in ('a', 'b', 'c', 'd'):
        call(flow.TASKS, 'add', '--project', 'night-test', '--slug', slug, '--title', slug)

    # Real CLI operations in separate processes, with a slow task lookup to widen the
    # read/modify/write race. No implementation lock or state function is mocked.
    runner = tmp_path / 'slow-night.py'
    runner.write_text(
        'import importlib.util\nimport time\n'
        f'spec = importlib.util.spec_from_file_location("night", {str(NQ)!r})\n'
        'module = importlib.util.module_from_spec(spec)\n'
        'spec.loader.exec_module(module)\n'
        'original = module.run_json\n'
        'def slow(*args):\n'
        '    time.sleep(0.1)\n'
        '    return original(*args)\n'
        'module.run_json = slow\n'
        'raise SystemExit(module.main())\n')

    def concurrent(commands):
        processes = []
        try:
            for args in commands:
                processes.append(subprocess.Popen(
                    [sys.executable, str(runner), args[0], '--project', 'night-test', *args[1:]],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            results = []
            for proc in processes:
                out, err = proc.communicate(timeout=20)
                results.append((proc.returncode, out, err))
            return results
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

    state = vault / 'raw/tasks/night-test/night.json'
    return call, concurrent, state


def test_concurrent_plan_accepts_only_one_run(queue_cli):
    _, concurrent, state = queue_cli
    results = concurrent([['plan'], ['plan']])
    assert sorted(rc for rc, _, _ in results) == [0, 3], results
    assert any('이미 running' in err for _, _, err in results)
    assert sorted(json.loads(state.read_text())['queue']) == ['a', 'b', 'c', 'd']


def test_concurrent_next_preserves_limit_and_record_preserves_every_result(queue_cli):
    call, concurrent, state = queue_cli
    call(NQ, 'plan', '--project', 'night-test', '--parallel', '3',
         '--slug', 'a', '--slug', 'b', '--slug', 'c', '--slug', 'd')
    results = concurrent([['next'] for _ in range(6)])
    assert all(rc == 0 for rc, _, _ in results), results
    assigned = [out.split('slug=', 1)[1].split()[0]
                for _, out, _ in results if 'PJ_NIGHT=next' in out]
    assert sorted(assigned) == ['a', 'b', 'c'], results
    assert sum('PJ_NIGHT=busy' in out for _, out, _ in results) == 3
    assert sorted(json.loads(state.read_text())['inflight']) == ['a', 'b', 'c']

    results = concurrent([['record', '--slug', slug, '--outcome', 'failed',
                           '--reason', f'test-{slug}'] for slug in assigned])
    assert all(rc == 0 for rc, _, _ in results), results
    saved = json.loads(state.read_text())
    assert not saved['inflight']
    assert sorted(saved['results']) == ['a', 'b', 'c']
    assert all(saved['results'][slug]['reason'] == f'test-{slug}' for slug in assigned)
    assert 'PJ_NIGHT=next slug=d' in call(NQ, 'next', '--project', 'night-test')
