<a id="sms-workflow-and-measurement-feasibility--2026-11-19"></a>
# SMS 워크플로와 측정 가능성 검증 — 2026-11-19

**합성 검증은 통과했고 운영 환경 통합은 미구현이다.** 내용 수준의 작업 계약에 앞서 워크플로 연결과 측정 가능한 평가를 우선했다. 모델 호출, 실제 사업 조사, 외부 메시지, 새 인프라는 사용하지 않았다.

함께 사용한 runtime fixture revision은 `3169a9b7a6520144429db3ab015617c6e86cf5d1`이다. 선택적 저장소 간 CI 작업은 이 fixture를 빌드한다. 당시 비공개 저장소 읽기 권한을 별도로 설정해야 했다.

<a id="executed-experiment"></a>
## 실행한 실험

`experiments/workflow_probe.py`는 실제 LangGraph/SQLite 체크포인트를 사용하고 별도 프로세스에서 Go의 `experiments/workflowprobe` fixture를 호출한다. Go fixture는 기존 `engine.Store`의 협업 영수증, lease, 대기 상태 전이, mailbox/outbox 대사를 재사용한다. 이 기능을 Python에 복제하지 않았다. fixture는 합성 실험용이며 범용 RPC API나 배포된 listener가 아니다.

Python 통합 테스트 5건이 19.136초에 통과했다.

| 사례 | 관측 결과 |
| --- | --- |
| 정상 문서 인계, 프로세스 재시작, SMS 반복 | 그래프 완료. 체크포인트의 고유 완료 수는 2, consumer generation은 2(대기 + 소비)를 유지했다. |
| correlation 불일치와 작성 작업 완료 전 SMS | 일치하는 알림과 작성자 완료 영수증을 모두 확보할 때까지 그래프 재개를 차단했다. |
| 산출물 누락 또는 변조 | 재개를 차단하고 영수증 SHA-256에 맞는 바이트를 복원한 뒤 진행했다. |
| 작성자를 실패·불확실 상태로 격리한 뒤 SMS 도착 | 그래프가 대기를 유지했다. |
| consumer 영수증 커밋 후 그래프 체크포인트 저장 전 종료 | 새 Python 프로세스가 캐시 영수증으로 완료했다. consumer lease나 실행이 추가되지 않았다. |

그래프 poll마다 새 Python 프로세스를 시작했다. 장애 사례는 `os._exit(17)`로 프로세스를 실제 종료했다. Go가 작성자 완료 영수증, fixture 산출물 해시, 정확한 발신자·수신자·correlation·메시지 식별자를 함께 확인한다. 완료 작업을 반복하면 영수증을 조회한다. 테스트에서는 consumer generation이 추가되지 않았음을 확인했다.

사용량과 비용은 null이다. 체크포인트에서 논리적 완료의 중복을 제거할 수 있지만 일반적인 시도·시간·재시도·토큰 집계는 구현하지 않았다. 이 다섯 사례는 최초의 실행 가능한 회귀 테스트이며 모델 품질은 평가하지 않았다.

<a id="findings-that-determine-implementation"></a>
## 구현에 반영할 결과

1. 기존 mailbox 일치만으로 대기 중인 runtime 작업을 준비 상태로 전환할 수 있다. 선행 작업 완료나 산출물 존재 여부는 증명하지 않으므로 그래프 재개 전에 영수증과 산출물을 확인해야 한다. fixture digest는 바이트 일치만 확인한다.
2. 영수증 기록과 그래프 체크포인트는 별도 트랜잭션이다. 고정된 task ID와 영수증 재사용으로 실험한 장애 구간은 복구했지만 임의의 외부 효과에 대한 exactly-once 보장은 없다.
3. Go 상태의 소유자는 하나다. 이 실험에서는 작업 사이에 store를 닫는다. 운영 중 동시 오케스트레이션에는 실행 중인 runtime 프로세스에서 알림·영수증 조회·승인 및 대사를 담당할 작은 API가 필요하다. Python에서 실행 중인 runtime 저장소를 별도로 열거나 lock을 우회해서는 안 된다.
4. runtime의 `Status`는 최신 상태 스냅샷이다. 스냅샷이나 벽시계 기준 lease 만료 시각만으로 시도 횟수, 수명주기 이력, 단계별 경과 시간을 신뢰성 있게 복원할 수 없다. 시도·지연 집계 전에 안정적인 event/run/task/attempt ID와 한도가 있는 영속 수명주기 기록이 필요하다.
5. 현재 어댑터의 Usage는 성공 턴만 요약하며 실패 턴의 사용량이 누락될 수 있다. 비용·사용량 집계에는 시도별 영속 기록, 완전성 표시, 모델 식별자와 가격 기준 버전이 필요하다. 사용량 누락은 0이 아니며 SDK/harness 재시도를 새 논리 작업으로 계산해서는 안 된다.
6. 관측·exporter 상태는 실행의 기준이 아니다. 이전 exporter 장애 테스트가 계속 적용된다. 이 실험은 Langfuse에 의존하지 않으며 추가적인 내구성을 보장하지 않는다.

<a id="next-implementation-slice"></a>
## 후속 구현

- Go 소유 프로세스를 통해 제한된 mailbox·영수증 조회와 알림 전달을 제공한다. 기존 SMS 식별 규칙과 단조 증가하는 lease 보호를 유지한다.
- 그래프의 의존성·재개 식별자를 저장하고 재개 시 영수증과 산출물을 다시 검사한다. 진행 상태를 영속화한 뒤 승인하고 재시작 후 대사한다. 중복·지연·순서 역전 알림이 동일한 논리적 결과로 수렴해야 한다.
- 수명주기·사용량 영속 원장과 중복을 제거하는 읽기 전용 집계를 추가한다. 논리 작업, 실행 시도, 캐시 전달, 시작 재시도를 구분한다. 누락된 사용량과 알 수 없는 가격은 미상으로 표시한다. 추정치는 구독 청구 금액이 아니다.
- 다섯 fixture를 재사용하고 동시 전달, 승인 시점 장애, 사용량 기록 순서 역전, exporter 장애를 자동 평가 runner에 추가한다.

동적 계획, 상세 내용 계약, memory paging, 업무 품질 평가는 보류했다. 이번 기술 검증은 후속 구현의 근거이며 전체 파이프라인, 완전한 비용 집계, 자율 연구의 성공까지 확인한 것은 아니다.

<a id="reproduce"></a>
## 재현

```sh
# role-runtime
./scripts/go.sh build -o bin/workflow-probe ./experiments/workflowprobe
# role-orchestrator
ROLE_WORKFLOW_PROBE_BIN=../role-runtime/bin/workflow-probe \
  .venv/bin/python -m unittest discover -s tests -p test_workflow_probe.py -v
```

테스트는 임시 상태를 사용하고 기존 실행을 변경하지 않는다. fixture 바이너리가 없으면 선택적 테스트를 건너뛰므로 통과로 집계하지 않는다. fixture는 단일 소유자와 작은 로컬 합성 산출물을 전제한다. 동시 호출자나 신뢰할 수 없는 임의 경로는 지원하지 않는다.
