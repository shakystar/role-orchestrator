# SMS 워크플로와 지표 v1 구현

2026-11-19. 첫 구현에서 runtime 소유 프로세스의 제어 인터페이스, 영속 알림 inbox, LangGraph 재개·ack, runtime 수명주기 기록, 독립 지표 집계와 고정 수용 검사 runner를 연결했다. 합성 작업으로 검증했으며 모델 호출과 업무 품질 평가는 수행하지 않았다. 기본 CLI는 기존 고정 파일럿을 사용한다.

함께 사용한 runtime 소스는 `19e41795ab4ea63005f60c00b92c126e25a79d4f`이며 선택적 hosted 통합 작업도 이 버전을 고정한다.

<a id="delivered-boundaries"></a>
## 구현 범위

| 모듈 | 구현한 동작 |
| --- | --- |
| Go control | runtime 소유 프로세스의 비공개 Unix socket, 연결 4개 제한, 크기가 제한된 페이지, 범위 검사, 영수증·산출물 조회, ack와 불변 후속 작업 제출 |
| Go lifecycle | 협업 영수증과 이벤트 intent 원자적 기록, 재생 가능한 영속 projection, 용량 예약, 안정적인 event ID, legacy 완전성 표시 |
| workflow.client | 크기를 제한한 JSON RPC, run/request 대응 검사, 명시적 오류 반환. 모델·작업 재시도 루프는 없음 |
| workflow.runner | SQLite inbox/cursor 트랜잭션, 상태 연결과 owner lock, 영수증·산출물 확인, LangGraph 재시작, 체크포인트 저장 후 ack |
| metrics.store | LangGraph/Langfuse 의존성 없이 SQLite 원시 이벤트·cursor·파생 projection을 한 트랜잭션에 저장. 중복·충돌 처리와 집계 재구성 지원 |
| evaluation | 명세의 19개 case ID를 실행 가능한 Python/Go 테스트에 연결. revision, dirty flag, 시간, 예상 결과, 검사 항목과 pass/fail/skip 기록. 실패나 건너뜀이 있으면 0이 아닌 코드로 종료 |

재개한 그래프가 기존 소유 프로세스에 후속 작업을 제출하도록 제어 계약에 `tasks.submit`을 추가했다. 설정한 consumer 주소만 허용한다. 작업 내용이 바뀌면 충돌로 처리하고 실패한 task ID는 다시 큐에 넣지 않는다. 이미 완료한 작업을 중복 제출하면 추가 실행 대신 캐시 전달 관측을 기록한다. 기존 JSONL bridge v1의 형식은 유지했다.

지표에는 고유 논리 작업, 실행 시도, 최종 상태, 시작 재시도 인덱스, 캐시 전달, 측정된 runtime 실행 시간과 큐·중단 구간 시간이 포함된다. 누락되거나 재시작에 걸친 구간은 불완전하다고 표시한다. `complete`는 **관측된 이벤트**의 일관성만 뜻한다. `coverage=observed_events_only`는 가능한 모든 원본 작업을 가져왔는지 보장하지 않는다는 표시다. 그래프 의존성 대기 시간, 시도별 토큰·금액은 사유와 함께 null로 둔다.

알림과 원본 이벤트는 Go에 보관한다. Python inbox와 지표 DB는 여기에서 파생한다. runtime cursor는 이벤트 시각이나 인과 순서를 나타내지 않는다. consumer는 읽기 cursor를 전진시키면서 이른 알림과 해결되지 않은 알림을 같은 트랜잭션으로 보존한다. 완료 여부는 runtime 영수증과 산출물 SHA-256으로 확인한다. Langfuse 수신이나 SMS만으로 완료 처리하지 않는다. runner는 한도가 있는 poll 명령이며 백그라운드 daemon이나 분산 scheduler는 구현하지 않았다.

<a id="remaining-limits"></a>
## 남은 범위

- 이 단계의 `runtime-workflow`는 fixture 호스트다. 재사용 가능한 control/lifecycle 패키지를 기존 실모델 진입점 전체에 연결하지 않았다. legacy bridge 영수증을 수명주기 원장에 소급 반영하지 않았다.
- 시도별 시작 재시도는 engine hook으로 기록한다. 수락된 작업 밖에서 생성된 backend는 특정 시도에 귀속시키지 않는다. 새 실모델 연결과 실제 토큰 사용 이벤트는 별도 통합 검사가 필요하다.
- 파일 원장은 한도를 두고 보관한다. 보존·아카이브 정책, 분산 reader, 운영용 인덱스 저장소는 후속 과제다. 논리적 용량 예약으로 실제 디스크 I/O 실패를 막을 수는 없다.
- 그래프·runtime별 소유자는 하나이며 임의의 외부 효과에 대한 exactly-once 보장은 없다.
- 내용 수준 작업 계약, 동적 팀과 DB memory paging은 후속 범위다.


<a id="reproduce-the-two-task-pipeline"></a>
<a id="acceptance-runner"></a>
<a id="verification-status"></a>
## 실행 및 검증 문서

- [두 작업 파이프라인 재현](runbooks/sms-workflow.md#reproduce-the-two-task-pipeline)
- [수용 검사 runner](runbooks/sms-workflow.md#acceptance-runner)
- [고정 커밋 조합의 검증 결과](validation/sms-workflow-v1.md#verification-status)
