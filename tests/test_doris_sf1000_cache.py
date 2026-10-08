"""Exercise production cache polling/config loading with fake yq/curl transports.

The yq fixture implements only the scalar queries used by load_config, reading
real suite YAML. No live Doris, network calls or cold-performance claim.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
YQ = r'''#!/usr/bin/env python3
import re,sys
expr,path=sys.argv[2:]
lines=open(path).read().splitlines()
def mapping(section):
    active=False; result={}
    for line in lines:
        if line == section+':': active=True;continue
        if active and line and not line.startswith((' ', '#')):break
        if active:
            m=re.match(r'  ([a-z_]+):\s*(.*?)(?:\s+#.*)?$',line)
            if m:
                value=m[2].strip()
                if value[:1] in ('"', "'") and value[-1:]==value[:1]:value=value[1:-1]
                result[m[1]]=value
    return result
if expr=='.':sys.exit(0)
if expr.startswith('.parameters '):
    for k,v in mapping('parameters').items(): print(k+'='+v)
elif expr.startswith('.engine.connection '):pass
elif expr.startswith('.paths.'):
    key=expr.split()[0].split('.')[-1];print(mapping('paths').get(key,''))
else:raise ValueError('Unexpected yq fixture expression')
'''
CURL = r'''#!/usr/bin/env python3
import json,os,sys
from urllib.parse import urlsplit
url=sys.argv[-1];host=urlsplit(url).hostname
with open(os.environ['CACHE_CALLS'],'a') as f:f.write(json.dumps({'host':host,'url':url})+'\n')
if '/api/file_cache?' in url:
    if os.environ.get('CLEAR_FAIL')=='1':print('fixture HTTP 500',file=sys.stderr);sys.exit(22)
    print('{"status":"OK"}')
else:
    values=json.loads(os.environ['CACHE_SIZES'])[host]
    if values is None:print('fixture unavailable',file=sys.stderr);sys.exit(7)
    print('# HELP file_cache_cache_size bytes')
    print('# TYPE file_cache_cache_size gauge')
    for i,v in enumerate(values):print('file_cache_cache_size{path="disk'+str(i)+'"} '+str(v))
'''

class SF1000CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name);self.calls=self.work/'calls.jsonl'
        for name,text in [('yq',YQ),('curl',CURL),('envsubst','#!/bin/sh\ncat\n')]:
            p=self.work/name;p.write_text(text);p.chmod(0o755)
        self.env={**os.environ,'PATH':str(self.work)+os.pathsep+os.environ['PATH'],
                  'BENCH_ROOT':str(ROOT),'CASE_WORK':str(self.work),'CACHE_CALLS':str(self.calls),
                  'CACHE_SIZES':json.dumps({'be-a':[0,0],'be-b':[0]}),
                  'CLEAR_FILE_CACHE_MAX_SIZE_GB':'2'}

    def run_shell(self,script,**env):
        pre='''set -euo pipefail
source "$BENCH_ROOT/benchmark.sh"
source "$BENCH_ROOT/engines/doris_engine.sh"
BE_HOSTS_ARR=(be-a be-b)
clear_file_cache=true; clear_sys_page_cache=false
clear_file_cache_timeout_min=0
user=root; password=; be_http_port=8040; be_brpc_port=8060
RESULT_DIR="$CASE_WORK"; TEST_ROOT="$CASE_WORK"
'''
        return subprocess.run(['bash','-c',pre+script],env={**self.env,**env},capture_output=True,text=True,timeout=10)

    def suite_script(self,suite,tail='clear_doris_file_cache'):
        return f'''CONFIG_FILE="$BENCH_ROOT/benchmarks/{suite}/sf1000/doris/benchmark.yaml"
load_config
clear_file_cache_max_size_gb="${{clear_file_cache_max_size_gb:-${{CLEAR_FILE_CACHE_MAX_SIZE_GB:-0}}}}"
printf 'effective_threshold=%s\\n' "$clear_file_cache_max_size_gb"
'''+tail

    def test_each_real_suite_rejects_residual_cache_despite_workflow_two_gb(self):
        for suite in ['tpch','tpcds','ssb','ssb_flat']:
            with self.subTest(suite=suite):
                r=self.run_shell(self.suite_script(suite),CACHE_SIZES=json.dumps({'be-a':[0,1073741824],'be-b':[0]}))
                self.assertNotEqual(r.returncode,0,r.stdout)
                self.assertIn('effective_threshold=0',r.stdout)
                self.assertIn('timeout waiting for file cache to drop to 0GB',r.stderr)

    def test_empty_be_list_cannot_qualify_as_cleared(self):
        r=self.run_shell(self.suite_script('tpch','BE_HOSTS_ARR=(); clear_doris_file_cache'))
        self.assertNotEqual(r.returncode,0)
        self.assertIn('no BE hosts',r.stderr)
        self.assertFalse(self.calls.exists())

    def test_invalid_metric_values_cannot_qualify_as_zero(self):
        for value in [-1, '-Inf', 'NaN', 'garbage']:
            with self.subTest(value=value):
                r=self.run_shell(self.suite_script('tpch'),CACHE_SIZES=json.dumps({'be-a':[value],'be-b':[0]}))
                self.assertNotEqual(r.returncode,0)
                self.assertIn('invalid file_cache_cache_size',r.stderr)

    def test_numeric_zero_in_scientific_notation_is_accepted(self):
        r=self.run_shell(self.suite_script('tpch'),CACHE_SIZES=json.dumps({'be-a':['0e+00'],'be-b':['0.00']}))
        self.assertEqual(r.returncode,0,r.stderr)

    def test_generic_non_suite_tolerance_still_accepts_one_gb(self):
        r=self.run_shell('clear_file_cache_max_size_gb=2; clear_doris_file_cache',CACHE_SIZES=json.dumps({'be-a':[1073741824],'be-b':[0]}))
        self.assertEqual(r.returncode,0,r.stderr)

    def test_zero_on_every_disk_and_be_succeeds_and_clears_each_be(self):
        r=self.run_shell(self.suite_script('tpch'))
        self.assertEqual(r.returncode,0,r.stderr)
        calls=[json.loads(x) for x in self.calls.read_text().splitlines()]
        self.assertEqual({x['host'] for x in calls if 'op=clear&sync=true' in x['url']},{'be-a','be-b'})
        self.assertEqual({x['host'] for x in calls if 'brpc_metrics' in x['url']},{'be-a','be-b'})

    def test_one_nonempty_be_fails_even_when_other_be_empty(self):
        r=self.run_shell(self.suite_script('ssb'),CACHE_SIZES=json.dumps({'be-a':[0,0],'be-b':[1]}))
        self.assertNotEqual(r.returncode,0)

    def test_missing_metrics_are_not_zero(self):
        r=self.run_shell(self.suite_script('tpcds'),CACHE_SIZES=json.dumps({'be-a':[],'be-b':[0]}))
        self.assertNotEqual(r.returncode,0)
        self.assertIn('metric not found',r.stderr)

    def test_http_failure_is_not_zero(self):
        r=self.run_shell(self.suite_script('ssb_flat'),CACHE_SIZES=json.dumps({'be-a':None,'be-b':[0]}))
        self.assertNotEqual(r.returncode,0)
        self.assertIn('curl exit=7',r.stderr)

    def test_clear_request_failure_stops_before_metrics(self):
        r=self.run_shell(self.suite_script('tpch'),CLEAR_FAIL='1')
        self.assertNotEqual(r.returncode,0)
        self.assertNotIn('brpc_metrics',self.calls.read_text())

    def test_nonzero_cache_prevents_any_measured_query(self):
        (self.work/'query').mkdir();(self.work/'query/q1.sql').write_text('SELECT 1;')
        script='''
QUERY_DIR=query; QUERY_MODE=file; profile=false; plan=false; db=test; ENGINE_TYPE=doris
cold_query_count=1; hot_query_count=2; query_times=3; clear_cache_scope=cold
run_timed_query() { printf 'SHOULD_NOT_RUN\\n'; RUN_QUERY_DURATION=0.125; }
run_query
'''
        r=self.run_shell(self.suite_script('tpch',script),CACHE_SIZES=json.dumps({'be-a':[0],'be-b':[1]}))
        self.assertNotEqual(r.returncode,0)
        self.assertIn('Failed to clear cache before query q1 cold run 1',r.stderr)
        self.assertNotIn('SHOULD_NOT_RUN',r.stdout)

    def test_one_cold_two_hot_only_clears_before_cold(self):
        (self.work/'query').mkdir();(self.work/'query/q1.sql').write_text('SELECT 1;')
        script='''
QUERY_DIR=query; QUERY_MODE=file; profile=false; plan=false; db=test; ENGINE_TYPE=doris
cold_query_count=1; hot_query_count=2; query_times=3; clear_cache_scope=cold
run_timed_query() { printf 'TIMED=%s\\n' "$3"; RUN_QUERY_DURATION=0.125; }
min_query_duration() { echo 0.125; }
run_query
'''
        r=self.run_shell(self.suite_script('tpch',script))
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(r.stdout.count('TIMED=cold_1'),1)
        self.assertEqual(r.stdout.count('TIMED=hot_'),2)
        calls=[json.loads(x) for x in self.calls.read_text().splitlines()]
        self.assertEqual(len([x for x in calls if 'op=clear' in x['url']]),2)

if __name__=='__main__':unittest.main()
