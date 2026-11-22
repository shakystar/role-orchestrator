# role-orchestrator

[role-runtime](https://github.com/shakystar/role-runtime)의 작업 흐름을 제어하는 Python LangGraph 실험 프로젝트입니다. 설계·실행·검증 자료는 [문서 색인](docs/README.md)에서 확인할 수 있습니다.

**최소 통합 파일럿이며 운영 도입 여부는 미정입니다.** LangGraph는 조사 그래프와 체크포인트를, Go는 실행 한도와 역할별 세션·작업 영수증의 영속 저장을, codex-adapter는 Codex 프로토콜을 담당합니다. 실제 모델 실행은 Go를 통해 로컬에서 인증한 Codex를 사용하므로 Python 모델 공급자나 API 키가 필요하지 않습니다.

```text
LangGraph + SQLite → JSONL → role-runtime Pool (one slot) → codex-adapter → Codex
```

검토자는 의도적으로 근거를 생략한 초기 주장을 두 가지 **합성** 사례와 대조해 수락하거나 수정을 요청합니다. 수정은 한 번만 허용하며 이후에는 수락하거나 판단을 보류합니다. 첫 검토 후 일부러 중단하고 별도 프로세스에서 재시작해 상태가 보존되는지 검사합니다. 이 중단은 업무 승인 절차와 무관합니다.

<a id="run"></a>
## 실행

Python 3.11 이상, uv, `cmd/runtime-bridge`가 포함된 role-runtime 체크아웃이 필요합니다.

```sh
cd ../role-runtime
./scripts/go.sh build -o bin/runtime-bridge ./cmd/runtime-bridge
cd ../role-orchestrator
uv sync --frozen
uv run --frozen python -m role_orchestrator start --state artifacts/fixture-01 --runtime ../role-runtime/bin/runtime-bridge
uv run --frozen python -m role_orchestrator resume --state artifacts/fixture-01 --runtime ../role-runtime/bin/runtime-bridge
uv run --frozen python -m role_orchestrator status --state artifacts/fixture-01 --runtime ../role-runtime/bin/runtime-bridge
uv run --frozen python -m unittest discover -s tests -v
```

fixture 백엔드는 **실제 Go 워커 풀 안에서** 미리 지정한 고정 응답을 사용해 구성요소 간 연결을 검사합니다. 모델 추론을 검사하려면 모든 명령에 `--backend codex --codex /absolute/path/to/codex`를 추가하고 새 상태 경로를 사용합니다. Codex 인증과 네트워크 연결이 필요합니다. macOS에서는 실행 명령 앞에 `caffeinate -i`를 붙이면 해당 명령이 실행되는 동안 유휴 절전을 막을 수 있습니다.

상태 디렉터리 하나에 한 실험과 그 고정 설정을 저장합니다. 기존 매니페스트가 있으면 `start`가 실행을 거부합니다. `resume`은 중단되거나 실패한 그래프를 이어서 실행합니다. 다만 영수증이 불확실하거나 실패를 나타내면 브리지가 해당 작업의 재실행을 거부합니다. 이 경우 명시적 복구가 필요하며 파일럿에는 복구 UI가 없습니다. `status`는 Go나 Codex를 시작하지 않습니다. 그래프 상태와 런타임 상태는 함께 보관합니다. 모델 출력과 세션 데이터는 로컬에 저장하며 Git에서 제외합니다.

<a id="scope-and-boundaries"></a>
## 범위와 제약

- 역할 3개와 실행 슬롯 1개를 사용하며 논리 작업은 최대 4건, 수정은 1회까지 허용합니다.
- CLI 호출마다 브리지 프로세스 하나가 순차 JSONL로 통신합니다. 한 호출 안에서는 여러 역할이 같은 Codex 프로세스를 재사용합니다.
- Python 프로세스를 재시작해도 SQLite 체크포인트가 유지됩니다. 작업과 변경 불가능한 역할 정의가 정확히 일치하면 성공한 Go 영수증을 재사용합니다.
- 외부 작업의 **정확히 한 번 실행은 보장하지 않습니다**. 모델이 작업을 끝낸 뒤 영수증을 영구 저장하기 전에 중단되면 안전하게 재실행할 수 있는지 판별할 수 없습니다.
- 동적 팀 생성, 웹 수집, 사업 수요 검증, SMS 기반 그래프 재개, 분산 스케줄링, 범용 취소 API, 메모리 페이징과 운영용 의사결정 정책은 이 파일럿의 범위 밖입니다.
- 모델 턴에 고정 제한 시간을 두지 않습니다. 어댑터 상태와 하트비트는 로컬 `runtime-status.jsonl`에 기록하며 파일럿 클라이언트는 동기적으로 기다립니다. 운영용 상태 감시는 제공하지 않습니다.
- `fcntl` 잠금을 사용하는 macOS/Linux 로컬 단일 사용자 실험입니다. Windows 지원은 구현하지 않았습니다.
- 반환된 근거 ID가 허용된 집합에 있는지 확인합니다. 근거의 진위나 해당 근거가 결론을 뒷받침하는지는 검사하지 않으므로 모델 판단은 틀릴 수 있습니다.

[프로토콜](docs/protocol.md), [검증 기록](docs/validation.md), [의존성](THIRD_PARTY.md)에 세부 사항이 있습니다. 정식 채택과 상세 제품 기획은 파일럿 이후 별도로 결정합니다.

<a id="ci-access"></a>
## CI 접근 권한

role-orchestrator와 role-runtime은 공개 저장소입니다. 기본 CI는 Python 그래프·계약·관측 테스트를 실행합니다. 현재 CI 설정에서 저장소 간 Go 통합 작업을 실행하려면 `ROLE_RUNTIME_INTEGRATION`을 `true`로 설정하고 `ROLE_RUNTIME_READ_TOKEN`을 제공해야 합니다. [CI 범위와 접근 요건](docs/validation.md#hosted-ci-scope)에 필요한 설정이 있습니다. 건너뛴 통합 작업은 통과로 집계하지 않으며 로컬 통합 결과와 실제 모델 검증은 별도로 기록합니다.

<a id="opportunity-and-planning-stage"></a>
## 기회 검토와 기획

[기회 검토·후보 백로그](docs/planning/opportunities.md)에 관측 facade/Langfuse, 작업 계약, 제한된 후속 작업 제안, 컨텍스트 분리, 결과물 참조, 평가셋, 기억·스킬의 설계 후보를 등록했다. [관측 경계 초안](docs/planning/observability.md)과 [오픈소스 추출 근거](docs/planning/sources.md)를 함께 따른다. 문서/일부 소스 조사 결과이며 구현·정식 채택 완료를 뜻하지 않는다.

<a id="local-observability-feasibility-probe"></a>
## 로컬 관측 검증

[로컬 Langfuse Docker 실행 절차](infra/langfuse/README.md)와 [검증 기록](docs/langfuse-validation.md)에서 실제 fixture 그래프의 Go 호출을 Python 클라이언트 스팬으로 내보내고 Langfuse v4에서 다시 조회했습니다. v1 브리지는 W3C 추적 컨텍스트를 Go 요청·실행 스팬까지 전달합니다. 기본 CLI는 크기를 제한한 메타데이터 전용 로컬 스팬 로그를 남깁니다. 루프백 주소의 Langfuse로 내보내려면 `--telemetry-env infra/langfuse/.env`를 추가합니다. 실제 모델 비용과 운영 환경의 관측은 범위 밖입니다. [계약·추적 검증](docs/contract-trace-validation.md)

버전이 없던 이전 실행을 브리지 v1에서 다시 시작하려면 새 상태 디렉터리가 필요합니다. 격리된 디렉터리에서 기존 Go 모드를 명시적으로 사용할 수 있습니다. 영수증을 자동으로 이전하지 않습니다. 식별자·호환성·복구 범위는 [프로토콜](docs/protocol.md)에 있습니다.

<a id="workflow-feasibility"></a>
## 워크플로 타당성 검증

[SMS·체크포인트 및 측정 검증](docs/workflow-feasibility.md)에는 실제 Go 저장소와 LangGraph를 사용한 재시작·알림·산출물 합성 검사 5건을 기록했습니다. 별도 실험이며 기본 CLI는 고정된 파일럿을 실행합니다. 운영용 SMS 기반 재개와 범용 지표·사용량 집계는 후속 구현 대상입니다.

[워크플로·지표 v1 준비 문서](docs/planning/workflow-metrics-v1.md)는 다음 구현에서 각 모듈이 담당할 기능, 로컬 제어 RPC, 재개와 수신 확인 순서, 영속 이벤트에서 파생할 상태와 수용 기준 19건을 정의합니다. 이 문서에 있는 기준은 실행 전 명세입니다.

<a id="owner-process-workflow-and-metrics"></a>
## 소유 프로세스 기반 워크플로와 지표

[구현과 실행 절차](docs/workflow-implementation.md)에 설명한 별도 워크플로 CLI는 상태를 소유한 Go 프로세스를 통해 SMS·영속 수신함·그래프 체크포인트·런타임 영수증을 연결합니다. 독립 지표 모듈과 수용 사례 19건의 실행기도 포함합니다. 이 호스트는 명시적으로 지정한 합성 과제를 사용합니다. 운영 및 모델 기반 배포와 토큰·비용 집계는 별도 범위입니다.

<a id="model-backed-workflow-slice"></a>
## 실제 모델 기반 워크플로

같은 SMS·제어·체크포인트 경로에서 Go를 통해 Codex 작성자와 검토자를 순서대로 실행할 수 있습니다. [구현과 실제 모델 검증](docs/model-workflow-validation.md)에 합성 수용 검사, 실제 모델 실행, 재시작 시 결과 재사용, 로컬 Langfuse 조회를 구분해 기록했습니다. 설정 파일은 `examples/model-document-workflow.json`이며 기본값은 fixture 모드입니다.

모델 워크플로에서 `--verify`를 사용하면 영수증과 연결된 스냅샷을 내보내 별도로 [M1 실행 후 인계 평가](docs/m1-handoff-check.md#operational-handoff-to-m1)를 수행할 수 있습니다.

`--semantic-model codex-adapter/gpt-6.2-sol`을 추가하면 해시 검사와 함께 산출물이 원문 근거에 충실한지 평가합니다. [채점기 설정·결과·사람 검토 사례](docs/m1-handoff-check.md#optional-semantic-grading)

원문 충실도와 요청 사항 충족도를 별도로 채점하려면 사전에 정의한 `--requirements` 파일을 추가합니다. [두 축의 채점과 g04 보정](docs/m1-handoff-check.md#faithfulness-and-completeness)

[작업 계약](docs/work-contract-proposals.md)을 제공하면 문서 검토자가 근거와 함께 후속 조치를 기록할 수 있습니다. [합성 여행 기획 계약](examples/discovery-contract.json)
