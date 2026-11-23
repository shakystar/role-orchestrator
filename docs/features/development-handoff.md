# v0.1.1 개발 인계 계약

개발 역할을 실행하면 Go 런타임은 시도와 실행 결과를 기록하고 오케스트레이터는 배정 당시의 입력·출력 계약으로 결과를 판정합니다. 산출물을 생산하는 역할의 성공에는 검증된 JSON 결과와 그 해시가 필요합니다.

## 개발 과제 한 건

Python 소스 변경을 목표부터 시작할 때는 `case_id`, `goal`, 기준 Git 커밋 전체 SHA인 `base_sha`, 저장소 경로인 `source.path`를 제공합니다. 변경 범위가 정해져 있으면 `source.files`에 기준 커밋의 Python 경로를 지정합니다. 생략하면 시나리오 담당자가 추적된 Python 파일 목록에서 작업에 필요한 소스를 고릅니다. 실행 명령은 아래와 같습니다.

```sh
python -m role_orchestrator.development_case run \
  --state "$STATE" --runtime "$BRIDGE" --lab "$LAB" \
  --revision "$ROSTER_SHA" --backend codex < goal.json
```

이 경로는 명부의 시나리오·동일성·중단 상태·검증 사례 담당자에게 순서대로 배정합니다. 독립 검토자가 검증 사례의 해시를 수락하면 회귀 검사 작성자와 Python 패치 작성자가 작업합니다. 패치 검토는 별도 세션에서 진행하며 성공 상태는 `patch_reviewed`입니다. Go 변경이 필요한 과제는 이 Python 경로의 적용 범위 밖입니다.

검증 사례 검토자가 현재 해시를 지정해 `revise`를 반환하면 사례 작성자가 해당 지적을 받아 최대 두 차례 수정합니다. 검토자는 수정본의 해시를 다시 판정합니다. 이전 사례와 검토 영수증은 상태 디렉터리에 보존됩니다. `blocked` 또는 다른 해시에 대한 판정은 이 보완을 시작하지 않습니다.

패치 검토의 원본 실행이 끝났으나 응답이 출력 계약을 위반한 경우에는 런타임 영수증의 작업·담당자·입력을 대조한 후 같은 패치에 대한 판정 형식을 최대 두 차례 보완합니다. 불확실한 실행은 다시 제출하지 않습니다.

격리 실행기는 역할이 제출한 테스트와 별도로 준비한 독립 검사를 각각 기준 소스와 후보 패치에 적용해 종료 코드와 로그를 보존합니다. 다음 명령은 패치·테스트·영수증·로그 해시를 대조한 뒤 명부의 증거 검토자에게 인수를 배정합니다. 두 검사 모두 기준 실패와 후보 통과를 기록하고 증거 검토자가 같은 기준을 통과로 판정해야 `accepted`를 반환합니다. 아래 입력은 권한 있는 실행기가 생성한 결과를 사용합니다.

```sh
python -m role_orchestrator.development_acceptance run \
  --state "$STATE" --runtime "$BRIDGE" --lab "$LAB" --revision "$ROSTER_SHA" \
  --backend codex --execution execution.json --oracle oracle.py \
  --baseline-log baseline.log --candidate-log candidate.log \
  --role-baseline-log role-baseline.log --role-candidate-log role-candidate.log < goal.json
```

### 기존 작성·검토 입력

Python 패치 작성자 `acl-007`과 독립 검토자 `acl-005`가 한 과제를 이어서 진행합니다. 입력 파일은 `case_id`, `criterion`, `author` 객체 필드를 가집니다. `case_id`는 문자열이고 `criterion.id`는 검증할 조건의 이름입니다. `author`에는 목표 문자열 `goal`, 전체 소스 커밋 `base_sha`, 그리고 `source`, `regression`, `acceptance`, `output_contract` 객체를 넣습니다. 코드와 검사 조건은 주어진 자료 안에서 읽을 수 있어야 합니다.

```sh
python -m role_orchestrator.development_case run \
  --state "$STATE" --runtime "$BRIDGE" --lab "$LAB" \
  --revision "$ROSTER_SHA" --backend codex < case.json
```

동일 입력으로 `run` 대신 `status`를 사용하면 새 작업을 제출하지 않고 결과를 조회합니다. 작성한 diff, 기준 커밋, 패치 SHA-256과 독립 검토 결과를 반환합니다. `accept`는 해당 패치에 대한 검토 판정입니다. 실제 적용 가능 여부는 고정 소스에서 같은 해시의 패치를 적용한 뒤 지정한 반례를 실행해 확인합니다.

## 배정과 조회

`python -m role_orchestrator.assignment`는 `--lab`, `--revision`, `--member`, `--task`, `--runtime`, `--backend`, `--state`를 받습니다. stdin에는 역할 입력 JSON 객체를 전달합니다. `--revision`에는 lab 명부의 전체 Git SHA를 사용합니다. 일반 개발 역할은 기본 프로필 `lab-v1`을 사용합니다. `development-patch-v1`은 Python 변경 담당자와 독립 검토자의 패치 작성·검토 계약입니다. 실행 계획에 실제 입력 해시와 선택한 역할 계약을 저장합니다. [역할 및 프로필별 필드](../../role_orchestrator/role_contracts.py)

정상 종료 뒤에는 `python -m role_orchestrator.assignment status --state STATE`로 기록을 읽습니다. 영수증 저장 직후 프로세스가 종료되었다면 다음 명령으로 누락된 판정을 조회할 수 있습니다.

```sh
python -m role_orchestrator.assignment status --state STATE --runtime /path/to/runtime-bridge
```

조회는 입력을 다시 제출하지 않습니다. 실행 ID, 시도 ID, 담당자, 원래 지시, 입력 해시와 영수증이 일치할 때 결과를 반환합니다. 과거 계획에 입력 해시가 없고 당시 판정 파일도 없으면 기존 미확정 상태를 유지합니다.

## 인계와 복구

인계 실행기는 `python -m role_orchestrator.handoff_runner --runtime BRIDGE --state STATE --backend fixture|codex`이며 명령과 계획을 JSON으로 받습니다. `publish-initial`로 초기 개정을 등록하고, `validate`가 확인한 모든 구조 결함을 보완 요청 하나에 기록합니다. `repair`는 같은 요청의 기존 실행을 재사용합니다. 의미 검토로 반려된 개정은 수정 후 `accept-review`로 특정 개정·해시를 수락해야 합니다. `consume`은 수락한 개정으로 소비자를 실행합니다. 결과가 필수인 소비자는 산출물과 완료 영수증을 함께 확정합니다.

같은 관찰을 다시 표현해도 동일한 요구와 수락 조건이면 기존 미결 요청을 반환합니다. 같은 기준·필드에 양립할 수 없는 조건이 등록되면 해당 개정에서 새 보완을 시작하지 않습니다. 이미 시작한 작업은 영수증을 대조하며, 슬롯당 최대 2회·실행당 최대 6회 한도는 유지됩니다. 확인된 실패는 사유를 반환합니다. 영수증으로 결과를 확정할 수 없는 경우 자동 재제출하지 않습니다.

기존 manifest는 당시 계약으로 재조회합니다. 읽기 전용 작업과 이미 종결된 인계의 성공 판정은 원래 기록을 따릅니다. 입력·출력 원문과 전체 영수증을 포함한 실행 상태는 로컬 상태 디렉터리에 보관합니다.
