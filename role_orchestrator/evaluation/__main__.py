import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
PYTHON_CASES={
    'sms_normal':['test_live_workflow.LiveWorkflowTests.test_sms_normal'],
    'sms_duplicate':['test_live_workflow.LiveWorkflowTests.test_sms_duplicate'],
    'sms_early':['test_live_workflow.LiveWorkflowTests.test_sms_early'],
    'sms_scope':['test_live_workflow.LiveWorkflowTests.test_sms_scope'],
    'artifact_invalid':['test_live_workflow.LiveWorkflowTests.test_artifact_invalid'],
    'producer_uncertain':['test_live_workflow.LiveWorkflowTests.test_producer_uncertain'],
    'checkpoint_crash':['test_live_workflow.LiveWorkflowTests.test_checkpoint_crash'],
    'ack_crash':['test_live_workflow.LiveWorkflowTests.test_ack_crash'],
    'inbox_crash':['test_live_workflow.LiveWorkflowTests.test_inbox_crash','test_metrics.MetricsTests.test_transaction_rollback'],
    'active_query':['test_live_workflow.LiveWorkflowTests.test_active_query'],
    'event_duplicate':['test_metrics.MetricsTests.test_duplicate'],
    'event_conflict':['test_metrics.MetricsTests.test_conflict'],
    'event_reordered':['test_metrics.MetricsTests.test_reordered'],
    'event_gap':['test_metrics.MetricsTests.test_gap'],
    'rebuild':['test_metrics.MetricsTests.test_rebuild','test_live_workflow.LiveWorkflowTests.test_restart_and_rebuild'],
    'langfuse_down':['test_live_workflow.LiveWorkflowTests.test_langfuse_down','test_bridge.FailureBoundaryTests.test_export_failure_and_failed_task_replay'],
    'usage_missing':['test_metrics.MetricsTests.test_usage_missing'],
}
GO_CASES={
    'event_projection_crash':['TestLifecycleIntentRecovery','TestLifecycleProjectionIdempotence'],
    'capacity':['TestLifecycleCapacityReservesCompletion'],
}

def revision(path):
    return subprocess.check_output(['git','rev-parse','HEAD'],cwd=path,text=True).strip()

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime-source',type=Path,default=ROOT.parent/'role-runtime')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    spec=json.loads((ROOT/'contracts/workflow-metrics-cases.json').read_text())
    sys.path.insert(0,str(ROOT/'tests'))
    results=[];began=time.monotonic()
    for case in spec['cases']:
        name=case['id'];started=time.monotonic();detail=[]
        if name in PYTHON_CASES:
            suite=unittest.defaultTestLoader.loadTestsFromNames(PYTHON_CASES[name])
            r=unittest.TextTestRunner(verbosity=1).run(suite)
            status='skipped' if r.skipped else 'passed' if r.wasSuccessful() else 'failed'
            detail=[str(test) for test,_ in r.errors+r.failures]
            checks=PYTHON_CASES[name]
        elif name in GO_CASES:
            checks=GO_CASES[name]
            proc=subprocess.run([str(a.runtime_source.resolve()/'scripts/go.sh'),'test','-race','./internal/engine','-run','^('+'|'.join(checks)+')$','-count=1'],cwd=a.runtime_source,capture_output=True,text=True,timeout=120)
            status='passed' if proc.returncode==0 else 'failed'
            if proc.returncode:detail=[(proc.stderr+proc.stdout)[-4000:]]
        else:status='failed';checks=[];detail=['unmapped acceptance case']
        results.append({**case,'status':status,'checks':checks,'failures':detail,'elapsed_seconds':round(time.monotonic()-started,3)})
    report={'schema_version':1,'mode':'synthetic','model_calls':0,'python':platform.python_version(),
            'orchestrator_revision':revision(ROOT),'runtime_revision':revision(a.runtime_source),
            'orchestrator_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
            'runtime_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=a.runtime_source)),
            'orchestrator_tracked_dirty':bool(subprocess.check_output(['git','diff','HEAD','--name-only'],cwd=ROOT)),
            'runtime_tracked_dirty':bool(subprocess.check_output(['git','diff','HEAD','--name-only'],cwd=a.runtime_source)),
            'elapsed_seconds':round(time.monotonic()-began,3),'cases':results,
            'limitations':['No live model quality or cost evaluation.','Projection recovery uses injected filesystem failure and store reopen; Python checkpoint crash cases use process exit.']}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+'\n')
    counts={status:sum(c['status']==status for c in results) for status in ['passed','failed','skipped']}
    print(json.dumps({'report':str(a.output),**counts}))
    return 0 if counts['passed']==len(spec['cases']) else 1
if __name__=='__main__':raise SystemExit(main())
