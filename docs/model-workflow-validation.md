# 실제 모델을 연결한 SMS 워크플로 검증

2026-11-19. 이전 단계는 합성 작업으로 SMS·완료 영수증·체크포인트·집계를
연결했다. 이번 단계에서는 **동일한 경로로 실제 Codex 작성자와 검토자를 실행**했다.
실험용 고정 기술 문서이며 사업 조사나 사용자 수요 검증은 아니다.

| 상황 | 이전 | 현재 |
| --- | --- | --- |
| 문서 생성·검토 | 합성 호스트가 정해진 결과 반환 | 작성자가 문서 생성, 검토자가 실제 문서와 요구사항을 비교 |
| 작업 인계 | 합성 산출물로 인계 검증 | 실제 문서 해시·완료 영수증 확인 후 검토 작업 제출 |
| 초기 연결 오류 | 시작 재시도 기록용 엔진 hook | 실제 어댑터 재시도를 해당 작업 시도에 연결해 집계 |
| SMS 이전 실패 | 알림이 없으면 대기 상태만 확인 가능 | 그래프 poll 응답에 생산자·소비자 상태와 구조화된 실패 표시 |
| 재시작 | 합성 작업 재실행 방지 검증 | 실제 완료 영수증으로 재개, 추가 모델 호출 0회 확인 |

## 완료한 구현

- Go `workflowmodel`: 기존 codex-adapter를 주입하는 실행기, 워커별 지연 초기화,
  역할별 세션 키, 작성/검토 JSON 검증, runtime 소유 산출물·SMS 생성.
- Go cooperative receipt/lifecycle/control: 실패 stage/code/execution 보존,
  `not_started`와 불확실한 실행 구분, 시작 재시도 집계 연결.
- Go 관측: 실행/모델 부모·자식 span, 영속 trace ID, 제한된 로컬 기록과 선택적
  loopback OTLP export. Python 소켓 호출 span 연결은 이번 구현에 포함하지 않는다.
- Python: SMS가 없어도 실패를 조회하는 poll 응답, 실제 모델용 예제 설정,
  `scripts/check_model_workflow.py` 관찰·완료 검사 도구 및 실패 표시 회귀 검사.

## 실행 결과

Runtime source: `bba4f3a6afc9b4ebc6adaf1c010feeb5a5434d2b`.
Python 변경은 이 문서와 같은 커밋에 포함된다. 실행 당시 개발 작업 트리였으며,
이전 19개 수용 검사 보고서를 깨끗한 최종 소스 실행 기록으로 바꿔 쓰지 않는다.

1. 사전 doctor 호출은 `workspace_routing_timeout`으로 실패했다.
2. 새 실행 `model-1119a`에서는 어댑터의 기존 제한된 시작 재시도 1회 후 복구했다.
   방화벽·프록시·TLS·인증 우회 설정은 변경하지 않았다.
3. 실제 작성자와 검토자 모두 완료했다. 두 완료 상태가 같은 Codex PID를 가리켰다.
   역할별 세션은 분리했으며 워커 상한은 2였다. 동시 실행 성능 실험은 아니다.
4. 검토자는 `accept`를 반환했고 작성 문서의 SHA-256과 검토 참조가 일치했다.
   `accept`를 강제하지 않았다. `revise`도 유효한 완료 결과지만 자동 수정 루프는 없다.
5. 논리 작업 2, 실행 시도 2, 완료 2, 시작 재시도 1, 완료 결과 재조회 1,
   실패 0, SMS ack 1을 확인했다. 재조회는 그래프 consume 노드 재개 과정에서 발생했다.
6. runtime과 Python을 재시작해 다시 poll한 뒤 상태 로그·span·산출물·trace ID의
   해시가 모두 동일했다. 추가 모델 호출은 0회였다.
7. Langfuse 4.82.0에서 `/api/public/v2/observations`로 4개 span ID와 부모 관계를
   대조했다. input/output은 비어 있고 종료 시간이 있었다. 원문은 export하지 않았다.

실행 시도 합산 시간 약 49.65초는 초기 연결·출력 처리 등을 포함한 runtime 시도
시간이다. 모델 생성 시간이나 사용자 체감 파이프라인 시간으로 해석하지 않는다.
토큰·비용·그래프 대기 시간은 아직 null이며 사유가 명시된다.

[정제된 검증 결과](model-workflow-validation.json)에 지표와 readback 결과를 남겼다.
모델 원문, 세션, 인증 정보는 로컬 ignored 상태 경로에만 보관한다.

## 재현

기존 공식 Codex 인증과 설치를 사용한다. 두 저장소의 의존성 추가 설치는 없었다.

```sh
# role-runtime, 터미널 1: 새로운 짧은 상태 경로 사용
./scripts/go.sh build -o bin/runtime-workflow ./cmd/runtime-workflow
./bin/runtime-workflow --backend codex --state /tmp/model-demo \
  --run-id model-demo --codex /absolute/path/to/codex

# role-orchestrator, 터미널 2: 한 번씩 관찰하며 done=true까지 호출
PYTHONPATH=. .venv/bin/python scripts/check_model_workflow.py \
  --runtime-state /tmp/model-demo --state artifacts/model-demo --run-id model-demo

# 완료 후 비동기 trace flush를 기다린 다음 검증
PYTHONPATH=. .venv/bin/python scripts/check_model_workflow.py \
  --runtime-state /tmp/model-demo --state artifacts/model-demo --run-id model-demo --verify
```

poll은 장시간 모델 작업을 종료하거나 실패 작업을 다시 시작하지 않는다. 실패하면
영수증을 조사하고 상태를 보존한다. 완료 상태의 Go 호스트를 SIGTERM으로 종료한 뒤
같은 명령으로 재시작하고 `--verify`를 반복하면 완료 수와 span 수가 유지되어야 한다.
Langfuse export 환경은 [runtime 문서](https://github.com/shakystar/role-runtime/blob/bba4f3a6afc9b4ebc6adaf1c010feeb5a5434d2b/docs/model-workflow.md)를 따른다.

## 검사와 남은 경계

- 기존 합성 수용 검사 19/19 통과. 이번 실제 모델 결과와 별개다.
- Python 전체 39개 검사 통과. SMS·체크포인트 재개 및 이번 실패 상태 표시 포함.
- Go 전체 race/vet 통과. 추가 관측 검사도 race 통과: HTTP 503 exporter가 로컬
  span 기록을 없애지 않으며 재시작 시 trace ID가 유지된다.
- 문서 변조는 모델 호출 전 차단, 중복 JSON 필드는 모델 완료 후 실패로 기록,
  4가지 실행 상태의 실패 영수증·격리 정책은 주입 테스트로 검증했다.

현재 범위는 고정된 작성→검토 2개 작업이다. 임의 업무 계약, 동적 팀·재기획,
검토 의견에 따른 수정 반복, 토큰/비용 수집, 기억 페이징, 모든 legacy 실행 경로
편입과 장기 운영은 별도 단계다. 이번 성공으로 라우팅 서비스의 영구 복구나
에이전트의 광범위한 판단 품질을 입증한 것은 아니다.
