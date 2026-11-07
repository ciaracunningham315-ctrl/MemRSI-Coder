"""Detect redundant Coder inspections using completed tools in the active step."""
from __future__ import annotations

import re
from pathlib import Path


def _inspection(task, executor, name, arguments):
    try:
        root = task['repo_path']
        if name == 'edit_file':
            old_text = arguments.get('old_text')
            if isinstance(old_text, str) and old_text and old_text == arguments.get('new_text'):
                path = executor.path(root, arguments['path'])
                if path.is_file():
                    return 'no_op_edit',
        if name == 'read_file':
            path = executor.path(root, arguments['path'])
            start = arguments.get('start_line', 1)
            count = arguments.get('max_lines', 200)
            if type(start) is int and type(count) is int and start > 0 and count > 0:
                return 'read', path, start, count
        if name == 'run_command':
            command = arguments.get('command')
            if not isinstance(command, list) or not command or command[0] not in ('ls', 'true'):
                return None
            if any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in command):
                return None
            cwd = executor.path(root, arguments.get('cwd') or '.')
            if command == ['true'] and cwd.is_dir():
                return 'no_op',
            if command[0] != 'ls':
                return None
            flags, paths = set(), []
            for arg in command[1:]:
                if arg.startswith('-'):
                    if not re.fullmatch(r'-[adl1h]+', arg):
                        return None
                    flags.update(arg[1:])
                else:
                    path = Path(arg)
                    paths.append(executor.path(root, path if path.is_absolute() else cwd / path))
            return 'ls', tuple(sorted(flags)), tuple(paths or [cwd])
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return None


def _is_check(name, arguments):
    argv = arguments.get('command')
    return name == 'run_tests' or (
        name == 'run_command' and isinstance(argv, list) and len(argv) >= 4
        and isinstance(argv[0], str) and re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(argv[0]).name)
        and argv[1:3] == ['-m', 'py_compile']
    )


def _unittest_discovery(task, executor, name, arguments):
    """Describe a narrowly recognized unittest discovery invocation.

    Keep argv parsing deliberately small: only Python unittest discovery, one
    explicit start directory, optional verbosity, and the default test filename
    pattern are accepted. Unknown flags and patterns fail closed.
    """
    if name not in ('run_tests', 'run_command'):
        return None
    argv = arguments.get('command')
    if (not isinstance(argv, list) or len(argv) < 6
            or not all(isinstance(arg, str) and arg and '\x00' not in arg for arg in argv)
            or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(argv[0]).name)
            or argv[1:4] != ['-m', 'unittest', 'discover']):
        return None

    start = None
    pattern = 'test*.py'  # unittest discover's documented default
    explicit_pattern = False
    verbose = False
    index = 4
    while index < len(argv):
        option = argv[index]
        if option in ('-v', '--verbose') and not verbose:
            verbose = True
            index += 1
        elif option in ('-s', '--start-directory') and start is None and index + 1 < len(argv):
            start = argv[index + 1]
            index += 2
        elif option in ('-p', '--pattern') and not explicit_pattern and index + 1 < len(argv):
            pattern = argv[index + 1]
            explicit_pattern = True
            index += 2
        else:
            return None

    if start is None or pattern != 'test*.py':
        return None
    try:
        root = Path(task['repo_path']).resolve()
        cwd = executor.path(root, arguments.get('cwd') or '.')
        start_dir = executor.path(root, cwd / start)
        if not cwd.is_dir() or not start_dir.is_dir():
            return None
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return ('unittest_discover', argv[0], str(cwd), str(start_dir), pattern)


def _already_listed(request, inspected, target_dirs):
    _, flags, paths = request
    if flags == ('d',):
        return all(path in target_dirs for path in paths)
    for observed in inspected:
        if observed[0] != 'ls' or observed[1] != flags:
            continue
        listed = observed[2]
        if all(path in listed or (
            'd' not in flags and path.parent in listed and path.parent.is_dir()
            and path.is_file() and ('a' in flags or not path.name.startswith('.'))
        ) for path in paths):
            return True
    return False


def redundant_inspections(store, verifier, task, state, executor, step_id, calls):
    """Close batches that add no evidence after observed delivery and checking."""
    requests = [
        _unittest_discovery(task, executor, call.name, call.arguments)
        or _inspection(task, executor, call.name, call.arguments)
        for call in calls
    ]
    if not requests or any(request is None for request in requests):
        return False

    events = store.read_events(task_id=task['task_id'])
    step_start = next((e['seq'] for e in reversed(events) if e['event_type'] == 'plan.step.started'
                       and e['payload'].get('step_id') == step_id), None)
    if step_start is None:
        return False
    outcomes = {
        e['payload'].get('tool_call_id'): e
        for e in events if e['seq'] > step_start and e['event_type'] in ('tool.completed', 'tool.failed')
    }
    inspected = set()
    passed_check = False
    for event in events:
        if event['seq'] <= step_start or event['event_type'] != 'tool.started':
            continue
        payload = event['payload']
        name, arguments = payload.get('tool'), payload.get('arguments') or {}
        outcome = outcomes.get(payload.get('tool_call_id'), {})
        succeeded = outcome.get('event_type') == 'tool.completed'
        inspection = _unittest_discovery(task, executor, name, arguments)
        if inspection is None:
            inspection = _inspection(task, executor, name, arguments)
        if inspection:
            if inspection[0] == 'unittest_discover':
                # A later failed or mutating test supersedes earlier success;
                # do not use a stale pass to suppress another Coder request.
                inspected.clear()
                passed_check = False
                result = outcome.get('payload', {})
                if (succeeded and result.get('exit_code') == 0
                        and not result.get('file_paths')):
                    inspected.add(inspection)
                    passed_check = True
            elif succeeded:
                inspected.add(inspection)
            continue
        # Arbitrary commands invalidate prior observations. Their exit status
        # alone is neither a passing project check nor evidence of a write.
        inspected.clear()
        result = outcome.get('payload', {})
        if not succeeded or name == 'edit_file' or result.get('file_paths'):
            passed_check = False
        if _is_check(name, arguments) and succeeded and result.get('exit_code') == 0:
            passed_check = True

    # A fixed independent check will run after the report, even when the
    # Coder's earlier validation used a command we do not recognize.
    fixed_check_pending = state.get('acceptance_command') and not state.get('verification_attempts')
    only_no_op_edits = all(request[0] == 'no_op_edit' for request in requests)
    if not passed_check and not fixed_check_pending and not only_no_op_edits:
        return False
    delivery = verifier.delivery_result(task, state)
    if delivery['status'] != 'passed':
        return False
    target_dirs = {Path(path) for path in delivery['targets'] if Path(path).is_dir()}
    return all(
        request in inspected
        or request[0] in ('no_op', 'no_op_edit')
        or (request[0] == 'ls' and _already_listed(request, inspected, target_dirs))
        for request in requests
    )
