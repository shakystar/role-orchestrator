# 로컬 Langfuse 검증 — 2026-11-19

로컬 Docker 수신기, 크기를 제한한 메타데이터 전용 OTel 전송, 독립 REST 조회의 기술적 가능성을 확인했다. 업무 효용, 전체 파이프라인 E2E, 운영용 관측 facade 전반은 검증 범위 밖이다.

<a id="environment-and-actual-checks"></a>
## 환경과 실행 결과

- macOS의 Docker Engine 20.10.20 / Compose 2.12.1에서 Langfuse 4.82.0을 사용하고 공식 이미지 6개의 digest를 고정했다.
- 로컬 웹 주소는 http://localhost:3300이며 웹·MinIO 포트만 loopback에 공개했다. Docker VM은 CPU 6개, 메모리 약 8 GiB였다.
- Python OTel SDK와 OTLP/HTTP exporter는 1.45.2를 사용하고 설치 버전을 uv.lock에 고정했다.
- 기존 그래프·Go bridge 검사, 부모 span 관계·메타데이터 필터링, exporter 예외 격리, 한도가 있는 큐의 손실 집계, 원격 endpoint 거부를 포함한 로컬 unittest 8건이 통과했다.
- `langfuse-probe-03`에서 Go fixture 작업 4건을 완료했고 모델 호출은 0회였다. span 5개를 내보낸 뒤 v2 observation API로 5개 모두 독립 조회했다. 같은 trace 안에서의 부모 관계와 종료 시각, 입력·출력 payload가 없음을 확인했다.
- 통제한 장애 실험 `langfuse-outage-01`에서는 웹 서비스만 정지했다. fixture 작업 4건이 완료됐고 span 5개에서 전송 실패를 보고했다. 모델 호출은 없었다. 정리 단계에서 웹 서비스를 재시작했다.
- 복구 실험 `langfuse-recovery-01`에서 health 정상, 로그인 페이지 HTTP 200을 확인했다. 새 fixture 작업 4건을 완료하고 span 5개를 모두 조회했다. 부모 관계가 일치했고 입력·출력 payload는 없었다. 종료 시점에 로컬 서비스 6개 모두 실행 중이었다.
- Compose 설정과 로컬 문서 링크 검사를 통과했다. Cloud CI는 그래프와 exporter 단위 테스트만 수행하고 Docker 통합은 로컬에서 실행했다.

<a id="failures-retained"></a>
## 보존한 실패 기록

1. 처음 Docker API는 응답했으나 이미지 pull이 `desktop-containerd/containerd.sock: connection refused`로 실패했다. 실행 중인 컨테이너는 없었다. Docker Desktop 정상 종료 요청이 멈춰 관련 Docker 앱·VM 프로세스에 TERM을 보낸 뒤 재시작했다. 데이터 초기화, 방화벽·프록시·TLS 변경, 대체 mirror는 사용하지 않았다. 시작 완료 후 공식 이미지 다운로드와 서비스 health 검사가 성공했다. 이후 사용자가 지정한 네트워크 정책 우회 금지 조건도 실행 문서에 기록했다.
2. `langfuse-probe-01`에서는 웹 서비스 최초 시작이 늦어 health 검사가 timeout이었고 span 5개 전송이 실패했다. fixture 작업 4건은 완료했으나 수집 검사는 실패로 기록했다.
3. `langfuse-probe-02`에서는 span 5개를 수신했지만 최초 검증이 legacy `/api/public/traces`로 조회해 404를 받았다. 응답에 v4 events_only 모드가 명시됐다. 공식 API 정의를 확인하고 `/api/public/v2/observations`로 변경했다. 구형 API를 활성화하기 위해 배포 모드를 변경하지 않았다.

<a id="what-is-not-validated"></a>
## 검증 범위의 한계

Python client RPC span은 왕복 시간을 측정하며 Go scheduler나 모델 자체의 지연 시간은 분리하지 않는다. 이 시점에서 Go 내부 OTel 전파, 프로세스 간 trace 연속성, 실모델 사용량·비용, 캐시 telemetry 중복 제거, 영속적 재전송, 지속 부하는 미검증이다. 이번 실험에서는 기존 그래프의 동일 프로세스 체크포인트 재개를 사용했다. 이전 별도 프로세스 재개 실험은 별도 결과로 기록한다.

실행 상태는 LangGraph/Go 영수증으로 판단하고 Langfuse는 비동기 관측 결과를 수집한다. SDK 큐가 넘치면 telemetry가 유실될 수 있으며 테스트는 큐를 비운 뒤 카운터로 손실을 확인했다. exactly-once 전송이나 영속 spool을 보장하지 않는다. fixture 실험의 모델 비용은 미상·해당 없음이며 금액 0으로 측정하지 않았다.

<a id="minimal-live-check--2026-11-19"></a>
## 최소 실모델 확인 — 2026-11-19

LangGraph 노드 하나가 기존 Go bridge를 통해 실제 Codex에 합성 확인 응답 작업 1개를 제출했다. 워커 하나가 작업을 완료했다. harness 설정을 포함한 Python RPC 왕복 시간은 5.898초였다. root와 RPC client span 2개를 내보내 Langfuse에서 독립 조회했고 부모 관계, 종료 시각, 입력·출력 payload 부재를 확인했다. 재시도나 추가 모델 작업은 없었다. [측정값](langfuse-live-minimal-metrics.json)을 참고한다.

현재 bridge 응답이 usage를 제공하지 않아 토큰 사용량과 비용은 null이다. 이 검사는 client 경계에서 실제 모델 호출을 관측할 수 있는지만 확인했다. Go 내부 tracing과 비용 정확도는 검증하지 않았고 운영 실행 코드는 변경하지 않았다.
