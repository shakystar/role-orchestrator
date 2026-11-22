<a id="bridge-v1-contract-and-ownership"></a>
# Bridge v1 계약과 상태 소유권

버전이 없던 통신 형식을 v1으로 교체했다. 두 저장소에서 같은 요청 검증 벡터를 사용한다. Python은 `contracts/v1-vectors.json`, Go는 `cmd/runtime-bridge/testdata/v1-vectors.json`에 벡터를 보관하고 각각 독립적으로 검증한다. 통합 테스트에서는 실제 bridge를 실행한다.

<a id="compatibility"></a>
## 호환성

`runtime-bridge`는 기본적으로 `schema_version: 1`을 요구하며 버전이 없는 요청은 거부한다. `--allow-legacy`로 필드 4개의 구형 요청을 보내려면 legacy 전용 상태 디렉터리를 사용해야 한다. legacy와 v1 상태를 섞거나 기존 영수증을 자동 이관할 수 없으므로 v1은 새 상태 디렉터리에서 시작한다. 기본 Python 클라이언트는 항상 v1을 전송한다. 내부 호출 형식 `execute({id, role, instruction, input})`을 그대로 유지하고 전송 시 v1 메시지로 구성한다.

현재 역할 정의 버전은 `v1`, capability는 `advisory`다. 임의의 역할 버전, capability·출력 스키마 협상, 동적 작업 생성은 지원 범위 밖이다.

<a id="request"></a>
## 요청

```json
{"schema_version":1,"run_id":"run-1","attempt_id":"attempt-1","traceparent":"00-11111111111111111111111111111111-2222222222222222-03","id":"task-1","role":"reviewer","instruction":"Review supplied evidence.","input":"{\"evidence\":[]}"}
```

모든 필드가 필수이며 알 수 없는 키나 중복 키는 거부한다. `schema_version`은 정수 1이어야 하고 boolean은 허용하지 않는다. 나머지 값은 문자열이다. 식별자는 `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` 형식을 따른다. UTF-8 기준 instruction은 1~16384바이트, input은 1~65536바이트다. JSONL 프레임 한도는 128 KiB이며, Python에서 인코딩된 요청은 120 KiB까지 허용한다. `traceparent`는 W3C 버전 00, 0이 아닌 소문자 16진수 trace/span ID, 두 자리 16진수 flags 바이트를 지원한다. Python OTel의 random-trace flag `03`도 허용한다. 다른 traceparent 버전과 tracestate는 지원하지 않는다.

<a id="result"></a>
## 응답

```json
{"schema_version":1,"run_id":"run-1","attempt_id":"attempt-1","id":"task-1","status":"succeeded","text":"...","cached":false,"stats":{"submitted":1,"succeeded":1,"failed":0,"rejected":0,"maxActive":1,"maxQueued":1,"workers":1}}
```

응답은 요청의 run/task/attempt 식별자를 그대로 반환한다. `text`는 빈 값도 가능한 문자열이고 `cached`는 boolean이다. 통계 값은 0 이상의 정수이며 boolean을 허용하지 않는다. 캐시를 쓰지 않은 성공 응답에는 제출·성공 작업이 각각 하나 기록된다. 캐시된 성공 응답은 모든 통계 값이 0이고 새 작업을 실행하지 않는다. Python은 식별자 불일치, 미지원 버전, 알 수 없는 필드, 상태·캐시 통계의 불일치를 거부한다.

오류 응답에는 `status=error`, `cached=false`, `error_code`와 민감한 정보를 제거한 `error` 문자열이 있다. 현재 코드는 `invalid_request`, `conflict`, `execution_blocked`, `execution_failed`이다. 상세 실행 진단은 로컬에 남긴다. 구문 오류가 있는 요청은 반환할 식별자가 없을 수 있다. Python은 유효하지 않은 요청을 전송하지 않는다. 잘못된 JSON 프레임에는 작업을 시작하지 않고 오류를 반환하며, scanner 입력이 한도를 초과하면 bridge를 종료한다. 자동 재시도는 없다. 전송 성공 여부와 별도로 업무 결과는 그래프에서 검증한다.

<a id="identifier-lifetime"></a>
## 식별자 수명

| 식별자 | 소유권과 수명 |
| --- | --- |
| run_id | Python이 run-identity.json에 한 번 저장하고 Go가 상태 디렉터리를 해당 실행에 연결한다. |
| trace_id | run_id와 함께 저장하며 프로세스를 재개해도 재사용한다. Go는 같은 run에서 trace가 바뀌면 거부한다. |
| task id | 한 run에 속하는 불변 논리 작업을 식별한다. |
| attempt_id | Python이 run/task로부터 결정적으로 생성하고 Go가 배정 전에 하나의 작업에 영속적으로 연결한다. |
| span id | 활성화·전달·실제 실행마다 새로 생성하며 기존 span을 다시 열거나 재사용하지 않는다. |

같은 완료 작업을 다시 전달하면 최초 시도와 영수증을 반환한다. 같은 작업에 다른 attempt ID를 주거나 다른 작업에 기존 attempt ID를 재사용하면 거부한다. 수정한 작업에는 새 task ID가 필요하다. 실패했거나 실행 여부가 불확실한 작업은 attempt ID만 바꿔 재시도할 수 없다. 자동 복구는 미구현이다.

<a id="trace-topology-and-restart"></a>
## Trace 구조와 재시작

```text
orchestrator.activation (new root per process activation, same persisted trace_id)
 └─ orchestrator.rpc.client (new span per request)
     └─ runtime.request (Go server span, extracted W3C parent)
         └─ runtime.execute (only if actual executor runs)
```

캐시 조회는 client/request span과 cache-hit 이벤트·속성을 생성하지만 `runtime.execute`는 생성하지 않는다. 프로세스가 재개되면 같은 trace에 새 activation root를 생성한다. 한 trace에 여러 root가 생기는 것은 의도한 동작이다. 이전 span을 다시 열거나 프로세스 사이에 장수 root를 유지하지 않는다. fixture 실행은 실행 span으로 기록한다. 모델 생성, Codex 내부 span, 토큰·비용 집계는 범위 밖이다.

Python과 Go는 각각 메타데이터만 담은 로컬 span 파일을 최대 8 MiB까지 기록하고, 별도로 로컬 Langfuse에 일괄 전송할 수 있다. CLI의 `--telemetry-env infra/langfuse/.env`로 활성화한다. 내보내기 설정과 관계없이 run 식별자는 보존된다. Python은 명시한 로컬 telemetry 설정만 환경 변수로 Go에 전달한다. 키를 명령줄 인수나 manifest에 기록하지 않는다. 임의의 원격 exporter는 지원하지 않는다.

<a id="state-ownership-and-crash-boundary"></a>
## 상태 소유권과 장애 처리

- Python은 그래프 체크포인트, run 식별자, 그래프의 결정을 관리한다.
- Go는 불변 역할·프로젝트 정의, protocol/run 연결, task/attempt 실행 의도, 영수증과 harness 세션을 관리한다.
- OTel/Langfuse는 관측을 담당하며 작업 완료나 예산 집행을 결정하지 않는다.

Go는 배정 전에 불변 attempt intent를 기록한다. intent만 있고 영수증이 없으면 실행 여부가 불확실하므로 재전달을 차단한다. 일치하는 성공 영수증이 있으면 원래 그래프 체크포인트를 기록하지 못했어도 재사용한다. 실패·미완료·손상된 영수증은 거부한다. 상태 디렉터리의 소유자는 하나다. Go store lock이 bridge의 변경을 보호하고 Python CLI도 owner lock을 유지한다. Python Runtime을 직접 사용할 때는 단일 호출자를 전제하며 동시 클라이언트 API를 제공하지 않는다.

두 저장소는 트랜잭션을 공유하지 않는다. 성공 결과의 중복 실행을 방지하지만 외부 효과에 대한 exactly-once 보장은 없다. 자동 legacy 이관, 불확실 작업 복구 UI, 분산 조정, SMS를 통한 그래프 재개, 모델 재시도는 제공하지 않는다.

<a id="telemetry-failure-policy"></a>
## Telemetry 장애 정책

두 SDK의 큐는 span 64개, 배치는 16개로 제한한다. Go exporter timeout은 2초이며 재시도하지 않는다. Python은 SDK의 유한한 HTTP timeout을 사용한다. Go 종료 context는 3초, Python 로컬 HTTP exporter timeout은 2초다. timeout을 무시하는 외부 exporter의 종료 시간은 Python facade가 보장할 수 없다. 내보내기 실패는 로컬에 요약하며 runtime 영수증에 영향을 주지 않는다. 큐 초과, 디스크 쓰기 또는 내보내기 실패로 관측을 잃을 수 있다. 영속 재전송이나 exactly-once telemetry를 보장하지 않는다. 로컬 tracing 실패가 대체 작업을 시작해서는 안 된다.

<a id="read-only-observation-and-bounded-review"></a>
## 읽기 전용 조회와 검토 한도

`AttemptReader.read(attempt)`는 Go bridge의 버전이 지정된 조회 인터페이스를 호출한다. 원래 요청과 영수증의 바이트를 반환하거나 관측이 없음을 반환한다. runtime 저장 경로는 Go 소유자만 해석한다. 로컬 프레임 검증에는 제출과 같은 인코더를 사용하며, 잘못된 프레임은 bridge에 쓰기 전에 `RequestNotStarted`를 발생시킨다.

`workflow.review.run_review`는 작성·검토 순서, 선택적 수정 1회, 자료 주입과 인계 읽기의 일시 오류 처리를 담당한다. 호출자는 산출물 접근, 단계별 관측, 최대 1회 배정을 위한 journal을 제공한다. 조회 모드는 기존 관측이나 intent가 없는 첫 단계에서 멈추고 `prepared`를 반환한다. 남은 단계는 명시적으로 재개할 수 있다. 대체 읽기에 실패하면 원래 주입한 오류와 복구 결과를 함께 전달한다.
