"""Exercise actual shell entry points with a per-process MySQL connection stub.

Run with: python3 -m unittest discover -s tests -v
No database, downloads, or installed benchmark tools are required.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MYSQL = r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
sql=sys.stdin.read()
with Path(os.environ['MYSQL_CALL_LOG']).open('a') as f:
    f.write(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:],'sql':sql})+'\n')
# Session state is intentionally recreated for each client process.
cache_enabled=True
for statement in sql.split(';'):
    statement=statement.strip()
    if not statement:continue
    if statement=='SET enable_sql_cache=false':cache_enabled=False
    elif statement=='SET invalid_session=true':
        print('ERROR 1193: Unknown system variable invalid_session',file=sys.stderr);sys.exit(17)
    elif statement=='SELECT @@enable_sql_cache':print(int(cache_enabled))
    elif statement=='SELECT require_sql_cache_off':
        if cache_enabled:
            print('ERROR: session cache is still enabled in query connection',file=sys.stderr);sys.exit(18)
        print('query-result')
    elif statement=='SELECT broken':
        print('ERROR 1064: fixture syntax error',file=sys.stderr);sys.exit(19)
    elif statement=='select last_query_id()':print('query-id-123')
'''

class DorisSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        (self.work / 'session').mkdir()
        self.session = self.work / 'session/session.sql'
        self.session.write_text('SET enable_sql_cache=false;\n')
        self.log = self.work / 'connections.jsonl'
        for name, content in [('mysql', MYSQL), ('envsubst', '#!/bin/sh\ncat\n')]:
            path = self.work / name
            path.write_text(content)
            path.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.work)+os.pathsep+os.environ['PATH'],
                    'MYSQL_CALL_LOG': str(self.log), 'CASE_DIR': str(self.work),
                    'BENCHMARK_ROOT': str(ROOT)}

    def run_shell(self, command):
        return subprocess.run(['bash', '-c', '''
set -euo pipefail
source "$BENCHMARK_ROOT/benchmark.sh"
source "$BENCHMARK_ROOT/engines/doris_engine.sh"
TEST_ROOT="$CASE_DIR"
RESULT_DIR="$CASE_DIR"
fe_host=127.0.0.1; fe_query_port=9030; user=root; password=; db=test
session=true
'''+command], env=self.env, capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_settings_and_measured_query_share_each_connection(self):
        result = self.run_shell('''
engine_run_sql "$db" 'SELECT @@enable_sql_cache; SELECT require_sql_cache_off;'
engine_run_sql "$db" 'SELECT require_sql_cache_off;'
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]['pid'], calls[1]['pid'])
        for call in calls:
            self.assertEqual(call['sql'].count('SET enable_sql_cache=false;'), 1)
            self.assertLess(call['sql'].index('SET enable_sql_cache'), call['sql'].index('SELECT'))
        self.assertEqual((self.work / '.last_query_id').read_text().strip(), 'query-id-123')

    def test_session_without_final_semicolon_is_separated_from_query(self):
        self.session.write_text('SET enable_sql_cache=false')
        result = self.run_shell("engine_run_sql \"$db\" 'SELECT require_sql_cache_off;'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SET enable_sql_cache=false\n;\nSELECT', self.calls()[0]['sql'])

    def test_initial_session_setup_is_not_prepended_twice(self):
        result = self.run_shell('run_session')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0]['sql'].count('SET enable_sql_cache=false;'), 1)

    def test_disabled_session_keeps_query_connection_defaults(self):
        result = self.run_shell("session=false; engine_run_sql \"$db\" 'SELECT require_sql_cache_off;'")
        self.assertEqual(result.returncode, 18)
        self.assertNotIn('SET enable_sql_cache', self.calls()[0]['sql'])

    def test_explicit_session_opt_out(self):
        result = self.run_shell("engine_run_sql \"$db\" 'SELECT @@enable_sql_cache;' false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('SET enable_sql_cache', self.calls()[0]['sql'])

    def test_invalid_session_is_fatal_before_measured_query(self):
        self.session.write_text('SET invalid_session=true;\n')
        result = self.run_shell("engine_run_sql \"$db\" 'SELECT require_sql_cache_off;'")
        self.assertEqual(result.returncode, 17)
        self.assertIn('Unknown system variable invalid_session', result.stderr)
        self.assertEqual((self.work / '.last_query_id').read_text(), '')

    def test_session_expansion_failure_does_not_start_mysql(self):
        (self.work / 'envsubst').write_text('#!/bin/sh\nexit 23\n')
        result = self.run_shell("engine_run_sql \"$db\" 'SELECT require_sql_cache_off;'")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn('Failed to prepare session SQL', result.stderr)

    def test_timed_failure_exits_even_in_conditional_context(self):
        result = self.run_shell('''
# Bash disables errexit for functions called in conditionals. The failure must
# still stop the benchmark, rather than recording 9999 and continuing.
if run_timed_query q1 q1 cold_1 'SELECT broken;'; then
    printf 'incorrect-success\\n'
fi
engine_run_sql "$db" 'SELECT require_sql_cache_off;'
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture syntax error', result.stderr)
        self.assertIn('Query execution failed q1 on cold_1', result.stderr)
        self.assertNotIn('incorrect-success', result.stdout)
        self.assertEqual(len(self.calls()), 1)

    def test_successful_timed_query_still_records_duration(self):
        result = self.run_shell('''
date() { printf '1000\\n'; }
bc() { cat >/dev/null; printf '0.125\\n'; }
profile_supported=false
run_timed_query q1 q1 cold_1 'SELECT require_sql_cache_off;'
printf 'duration=%s\\n' "$RUN_QUERY_DURATION"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('duration=0.125', result.stdout)
        self.assertEqual(len(self.calls()), 1)

if __name__ == '__main__':
    unittest.main()
