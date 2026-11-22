# SMS 워크플로 실행 절차

로컬 Unix socket을 사용할 수 있는 환경과 인접한 role-runtime checkout을 사용한다. 명령은 각 저장소 루트에서 실행한다.

<a id="reproduce-the-two-task-pipeline"></a>
## 두 작업 파이프라인 재현

첫 번째 터미널에서 Go 호스트를 빌드하고 실행한다.

```sh
cd ../role-runtime
./scripts/go.sh build -o bin/runtime-workflow ./cmd/runtime-workflow
./bin/runtime-workflow --state /tmp/role-flow-demo --run-id demo --fixture-delay 2s
```

두 번째 터미널의 role-orchestrator 디렉터리에서 `done=true`까지 poll 명령을 실행한다. 매번 새 프로세스를 사용해도 inbox와 그래프 체크포인트는 유지된다.

```sh
.venv/bin/python -m role_orchestrator.workflow \
  --state artifacts/workflow-demo --run-id demo \
  --socket /tmp/role-flow-demo/control/runtime.sock \
  --config examples/document-workflow.json
```

한도가 있는 이벤트 페이지를 읽어 지표를 생성한다.

```sh
.venv/bin/python -m role_orchestrator.metrics \
  --state artifacts/workflow-demo/metrics.sqlite --run-id demo \
  --socket /tmp/role-flow-demo/control/runtime.sock
```

CLI는 페이지 한도 안에서 변하지 않는 cursor에 도달했는지 보고한다. 지표는 보관 중인 원본을 **새** 지표 DB로 다시 가져와 재구성한다. 원본 영수증을 삭제하거나 재시도하지 않는다. 새 실행은 새 상태 경로를 사용하고 불확실한 기록을 보존한다.

<a id="acceptance-runner"></a>
## 수용 검사 runner

```sh
# Build runtime-bridge too: the exporter-failure regression uses that real bridge.
.venv/bin/python -m role_orchestrator.evaluation \
  --runtime-source ../role-runtime --output artifacts/workflow-evaluation.json
```

[이 소스 조합의 검증 결과](../validation/sms-workflow-v1.md)에서 기대 결과와 검사 범위를 확인할 수 있다.
