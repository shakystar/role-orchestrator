# SMS 워크플로·집계 구현 준비안 v1

2026-11-19. 상태: **다음 구현의 계약·수용 기준 확정안, 서비스 구현 전**.
기존 [5개 합성 검증](../workflow-feasibility.md)은 연결 가능성의 근거이며 아래 API·원장·집계기의 구현 완료 증거가 아니다.
내용 수준 작업 계약, 동적 기획, 비용 계산, 별도 마이크로서비스는 이번 첫 구현에서 제외한다.

## 1. 배치·모듈 경계

| 위치 | 다음에 만들 모듈 | 책임 |
| --- | --- | --- |
| role-runtime | control | 실행 중인 소유 프로세스의 SMS·영수증·이벤트 조회와 ack |
| role-runtime | lifecycle | 실행 상태 변경과 함께 영속 이벤트 의도를 기록·복구 |
| role-orchestrator | workflow | 의존성 확인, 알림 수신함, LangGraph 대기·재개 |
| role-orchestrator | metrics | 이벤트 검증·중복 제거·SQLite 투영·재집계 |
| role-orchestrator | evaluation | 고정 사례 실행·기대값 비교·JSON 보고 |
| 기존 observability | exporter | 메타데이터를 OTLP로 Langfuse에 전송 |

metrics는 LangGraph·Langfuse를 import하지 않는다. 입력은 버전 있는 이벤트, 출력은 조회 결과다.
workflow는 metrics 결과로 작업 완료를 판정하지 않는다. 평가 runner는 fixtures와 공개 모듈 경계를 사용하며 실행 구현을 복제하지 않는다.
별도 저장소·컨테이너·DB 서버·메시지 브로커를 추가하지 않는다. Langfuse 공식 이미지와 digest 고정 배치를 유지한다.

## 2. 실행 중 runtime 제어 계약

현재 JSONL bridge v1은 순차 실행용이므로 변경하지 않는다. 첫 제어 경로는 **같은 runtime 소유 프로세스에 Unix domain socket**을 두는 로컬 전용 API로 한다.
소유 프로세스만 Store를 연다. Python이 runtime 상태 파일을 직접 열어 제어하지 않는다.
소켓은 소유자 전용 디렉터리/권한을 사용하며 TCP 포트를 공개하지 않는다. Windows 지원은 별도다.
기존 소유 잠금을 확인하지 않고 소켓을 삭제하거나 두 번째 runtime을 시작하지 않는다.

요청 공통: `version=1`, `request_id`, `run_id`, `method`, `params`.
응답 공통: `version`, `request_id`, `run_id`, `ok`, `result` 또는 `error{code,retryable}`.
알 수 없는 버전/필드, 중복 JSON 키, 잘못된 run·project·consumer는 거절한다. 오류에 원문이나 키를 포함하지 않는다.

| method | params / 의미 | 부작용 |
| --- | --- | --- |
| notifications.list | consumer_id, after_cursor(nullable), limit | 고정된 알림 목록 읽기; 전달됐다고 처리 완료로 간주하지 않음 |
| receipts.get | task_id | status, receipt_revision, attempt_id, 결과물 참조, 실행 실패 상태 조회 |
| notifications.ack | consumer_id, delivery_id, checkpoint_id | 해당 consumer의 처리 확인만 영속 저장; 동일 ack 반복은 성공 |
| events.list | after_cursor(nullable), limit | 집계용 이벤트 읽기; 원본 작업 상태 변경 없음 |
| health.get | 없음 | 소유 프로세스·원장 상태 조회; 모델 진전 보장은 아님 |

초기 구현 한도: 요청 64 KiB, 응답 256 KiB, 페이지 1..100개, 동시 제어 요청 4개,
연결당 유휴 읽기/쓰기 제한 5초. 초과 시 명시적으로 거절한다. 이는 파일럿 상한이며 성능 최적값이 아니다.
공통 오류 코드는 `invalid_request`, `unsupported_version`, `scope_mismatch`, `not_found`, `cursor_expired`, `conflict`, `busy`, `capacity`, `unavailable`로 제한한다. read 재시도는 가능하지만 ack는 동일 ID를 유지하며 자동 작업 재제출은 하지 않는다.

긴 모델 실행은 이 제어 경로에서 기다리지 않는다. 대기 중에도 bounded 조회가 가능한지 테스트한다.

delivery에는 `delivery_id`, 불변 message identity(sender/recipient/project/role version/correlation),
producer_task_id, 결과물 참조가 포함된다. 이때 **delivery_id는 전달 건, request_id는 통신 건, task_id는 논리 작업**이다.
동일 SMS의 재전달에도 delivery_id를 유지한다. 원문 message ID만으로 전역 중복 제거하지 않는다.
ack는 checkpoint의 진실성을 runtime이 인증한다는 뜻이 아니라 신뢰된 로컬 consumer가 진행을 기록했다는 의미다.

cursor는 불투명한 ledger 세대+위치이며 이벤트 시각·작업 순서를 뜻하지 않는다.
알림 수신함 저장과 cursor 갱신은 Python DB 한 트랜잭션으로 처리한다.
미완료 의존성의 알림은 수신함에 남겨 재검사하며, 다음 페이지를 읽어도 버리지 않는다.
retention으로 cursor가 만료되면 `cursor_expired`와 명시적 재조정 절차를 반환한다. 처음부터 읽은 것처럼 조용히 성공시키지 않는다.
첫 구현에서는 미처리 알림을 자동 삭제하지 않는다.

## 3. 재개·ack 순서와 장애 경계

1. 알림 페이지를 로컬 inbox에 upsert하고 cursor를 같이 커밋한다.
2. 기대 sender/project/role version/correlation을 확인한다.
3. runtime 완료 영수증 및 산출물 버전·해시·접근 범위를 확인한다. 경로는 임의 외부 경로 대신 허용된 참조를 사용한다.
4. 실패·불확실·자료 부족이면 blocked 이유를 저장하고 대기한다. SMS만으로 completed로 승격하지 않는다.
5. 기존 task ID로 그래프를 재개한다. runtime 성공 영수증은 재사용하며 실패 작업은 자동 재실행하지 않는다.
6. 그래프의 영속 체크포인트에서 해당 의존성 처리가 끝난 것을 확인한다.
7. inbox에 처리 완료를 저장하고 ack를 전송한다. ack 실패는 같은 delivery_id로 재전송한다.

체크포인트·inbox·runtime은 하나의 트랜잭션이 아니다. 단계 5~7 사이에 종료되면 체크포인트를 조회해 처리 상태를 복구한다.
알림 소비 기록만 먼저 쓰고 그래프 실행을 건너뛰지 않는다. receipt 이후 checkpoint 이전 종료 시 기존 검증처럼 receipt로 수렴한다.
하나의 graph instance는 한 소유자만 실행한다. epoch/generation이 다른 오래된 알림은 새 의존성을 충족하지 않는다.

## 4. 실행 이벤트 v1

공통 필드:

- `schema_version=1`, `event_id`, `run_id`, `task_id`.
- `attempt_id`: 논리 실행 시도. 캐시 조회는 원래 시도를 참조하고 새 시도를 만들지 않는다.
- `source=runtime`, `revision`: task별 증가하는 영속 revision. 서로 다른 task의 revision을 비교하지 않는다.
- `kind`, `occurred_at`(UTC), `payload`. ID·문자열·전체 크기 검증을 적용한다.
- `trace_id`/`span_id`는 선택적인 관측 상관관계다. exporter가 없어도 이벤트는 유효하다.

첫 kind: `task.accepted`, `task.waiting`, `task.ready`, `attempt.started`, `attempt.finished`,
`startup.retry`, `result.cache_hit`. `attempt.started`는 실행기 호출이며 모델 시작 확인과 구분한다.
`attempt.finished`는 outcome과 adapter의 execution 증거(not_started/unknown/started/completed)를 담는다.
초기화 재시도는 같은 attempt의 retry_index로 구분한다. task.ready 반복으로 새 논리 작업을 만들지 않는다.

시각은 정렬 보조용이다. 같은 프로세스에서 측정한 `duration_ns`만 기간으로 사용하고,
프로세스 재시작을 걸친 대기는 측정 구간 합과 completeness를 함께 반환한다.
서로 다른 프로세스의 단조 시각을 빼거나 누락 구간을 0으로 채우지 않는다.

원본 저장 규칙:

- task 상태·revision·미전송 event intent를 기존 atomic record 변경에 함께 기록한다.
- event_id는 intent 생성 시 확정한다. 별도 이벤트 원장으로 옮기다 종료돼도 같은 ID로 재시도한다.
- 원장 영속 저장 확인 전에 intent를 제거하지 않는다. 복구 시 남은 intent를 재조정한다.
- 두 파일에 차례로 써놓고 원자적이라고 주장하지 않는다. 기존 receipt 포맷에는 버전을 붙이고 이전 데이터는 과거 이벤트가 없는 것으로 표시한다.
- 실행 수락 전에 최대 terminal record/event 공간을 예약한다. 이미 실행 중인 작업의 종료 기록이 신규 이벤트로 인해 밀려나지 않도록 용량 예산을 분리한다. 예약·잔여 용량은 재시작 때 재계산하며 디스크 I/O 실패는 별도 복구 필요 상태로 드러낸다.
- 기록 공간 부족이면 새 실행 수락을 중단한다. 이미 끝난 실행을 관측 실패 때문에 재실행하지 않는다.
- 미전송 event intent가 있는 task의 다음 상태 변경은 안전하게 배출할 때까지 보류한다. 무제한 intent 배열을 만들지 않는다.
- pilot 원장 상한 기본 64 MiB, 이벤트 최대 16 KiB. 자동 잘라내기 대신 capacity 상태를 노출한다. archive/retention 정책은 별도이며 오래된 상태를 버리지 않는다.

## 5. 집계·재집계 규칙

SQLite 입력 테이블 PK는 `(source, run_id, event_id)`다. 같은 ID+같은 정규화 내용은 no-op,
같은 ID+다른 내용은 `event_conflict`로 격리하며 기존 투영을 덮어쓰지 않는다.
한 입력 트랜잭션에 raw event 보관·중복 판정·투영·cursor를 포함한다.
원본 runtime은 권위 있는 실행 기록이고, Python SQLite는 삭제 후 재구축 가능한 파생 저장소다.
다만 runtime 원장 보존 범위가 부족하면 완전 재집계가 불가능하다고 표시한다.

| 지표 | 계산·구분 |
| --- | --- |
| 논리 작업 수 | unique(run_id, task_id) |
| 실행 시도 수 | unique(run_id, task_id, attempt_id)의 attempt.started |
| 완료·실패 | task의 최신 유효 revision 및 attempt terminal outcome; 역순 입력으로 상태를 되돌리지 않음 |
| 초기화 재시도 | unique(attempt_id, retry_index); 모델 재실행 횟수로 부르지 않음 |
| 캐시 반환 | unique cache-hit delivery event; 작업/시도/토큰 수에 더하지 않음 |
| 대기·실행 시간 | 측정 구간의 동일한 phase/segment ID별 중복 제거; 중첩 span 합산 금지 |
| 불완전성 | revision 누락·기간 누락·기록 범위·충돌을 별도 표시 |

finished가 started보다 먼저 도착할 수 있다. terminal 상태를 유지하면서 시작 이벤트 누락을 표시하고,
나중에 도착하면 누락된 실행 시도 수와 completeness를 보정하되 terminal 상태는 유지한다. 상충하는 terminal outcome은 임의 선택하지 않는다.
집계는 정책 판단을 위한 조회값이며 예산 집행·작업 완료 권위가 아니다.

사용량·비용은 후속 slice다. attempt별 최종 누적값·측정 범위·모델 식별·누락 이유가 있어야 한다.
현재 성공 턴 누적 Usage를 전체 비용으로 환산하지 않는다. 초기 출력은 `usage=null`, `cost=null`, 이유를 명시한다.

## 6. Langfuse와의 계약

공식 이미지를 포크하거나 자체 이미지로 다시 빌드하지 않는다. 기존 OTLP exporter를 유지한다.
내부 이벤트 원장이 Langfuse 테이블·REST 응답 형식에 의존하지 않게 한다.
Langfuse v4 조회는 기존 `/api/public/v2/observations` 검증 경로를 쓰지만, 집계 원본으로 쓰지 않는다.
Langfuse 중단 시 workflow·원장·집계는 동작해야 한다. 전송 버퍼 유실은 별도 관측 완전성으로 표시한다.
계약 검증에 서버가 꼭 필요하지 않도록 local exporter/failing exporter 테스트를 유지한다.

## 7. 실행 순서·완료 조건

| slice | 산출물 | 완료 조건 |
| --- | --- | --- |
| A | runtime 제어 API + Python client | 실행 중 조회 가능, 페이지·동시성 상한, 잘못된 scope 거절 |
| B | durable inbox + graph wakeup/ack | 정상·중복·조기·역순·재시작·ack 손실 사례가 동일 결과로 수렴 |
| C | runtime event intent/원장 + metrics 모듈 | 중간 종료·중복·역순·누락·충돌·재집계 수용 시험 통과 |
| D | evaluation runner + 결과 보고 | 고정 입력·예상값·버전·통과/실패/skip을 기록하고 불일치 시 nonzero exit |

먼저 A→B→C→D로 하나의 문서 인계 흐름을 완성한다. fixture 2작업을 시작점으로 하되 동시 실행 중 API 응답성도 확인한다.
A~D 완료 전에 토큰·비용 집계, 동적 팀 편성, 프로덕션 운영 준비 완료를 주장하지 않는다.
수용 사례는 `contracts/workflow-metrics-cases.json`에 고정한다. 현재 파일은 평가 명세이며 runner 실행 결과가 아니다.
