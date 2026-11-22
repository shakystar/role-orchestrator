<a id="local-langfuse-feasibility-stack"></a>
# 로컬 Langfuse 검증 환경

Langfuse **4.82.0**의 [공식 Compose 구성](https://github.com/langfuse/langfuse/blob/6dfeb6049b8fa2795af7bfce1cce9bbe60aca4c0/docker-compose.yml)을 바탕으로 구성했습니다. compose.yaml과 [images.lock.json](images.lock.json)에서 공식 이미지 6개의 다이제스트를 고정합니다. 전용 Compose 프로젝트 `role-observability`에서 웹, 워커, PostgreSQL, Redis, ClickHouse, MinIO를 실행합니다. AI 게이트웨이와 모델 공급자는 설정하지 않습니다.

<a id="start-and-access"></a>
## 시작과 접속

role-orchestrator 루트에서 실행합니다.

```sh
python3 scripts/init_langfuse.py
# Existing .env is intentionally not overwritten; run init only once.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env up -d
curl --max-time 10 http://localhost:3300/api/public/health
```

상태 조회에서 `{"status":"OK","version":"4.82.0"}`가 반환된 뒤 검사를 시작합니다. 첫 데이터베이스 마이그레이션과 초기 기동에는 시간이 걸릴 수 있습니다. `up -d`가 끝나도 웹 서비스는 아직 준비 중일 수 있습니다.

**http://localhost:3300**에 접속합니다. 로그인 이메일은 `local@role-runtime.test`이며 생성된 비밀번호는 로컬 `infra/langfuse/.env`의 `LANGFUSE_INIT_USER_PASSWORD`에 있습니다. 프로젝트 이름은 `Orchestrator Synthetic Validation`입니다. `.env`에는 API 인증 정보도 들어 있습니다. 파일 권한은 0600이며 Git에서 제외합니다. 로그나 이슈에 내용을 붙여 넣지 않습니다. 스크립트는 Langfuse 공식 초기화 변수로 로컬 사용자와 프로젝트를 생성합니다.

호스트에 공개하는 포트는 웹 `127.0.0.1:3300`과 MinIO `127.0.0.1:3900`뿐입니다. 다른 데이터베이스와 큐의 포트는 Compose 네트워크 안에서만 접근할 수 있습니다. 호스트 디렉터리 마운트, 특권 컨테이너, 인증서 검사 해제, 대체 레지스트리, 방화벽·프록시 변경은 사용하지 않습니다. Langfuse 자체 원격 측정은 비활성화했으며 Docker 네트워크 설정은 변경하지 않았습니다.

검증 환경의 Docker Desktop VM에는 CPU 6개와 약 8 GiB 메모리가 할당됐습니다. 이는 검증 당시 환경이며 최소 자원 기준은 측정하지 않았습니다. 데이터는 이름 있는 볼륨에 보관합니다. 컨테이너 로그는 서비스별로 10 MB 파일 3개까지 순환 저장합니다. 이 파일럿은 데이터베이스와 애플리케이션 데이터 크기를 자동으로 제한하지 않습니다.

<a id="probe"></a>
## 검사 실행

루트 README의 절차대로 기존 Go 브리지를 빌드하고 고정된 Python 의존성을 설치한 다음 아래 명령을 실행합니다.

```sh
uv run --frozen python scripts/probe_langfuse.py \
  --runtime ../role-runtime/bin/runtime-bridge \
  --state artifacts/langfuse-new
```

매번 새 상태 디렉터리가 필요합니다. 검사는 모델을 호출하지 않고 고정된 fixture 응답으로 실제 LangGraph 그래프와 Go 풀을 실행합니다. OTel은 루트 스팬 하나와 **Python 측 RPC 클라이언트 스팬 4개**를 `/api/public/otel/v1/traces`로 전송합니다. 인증에는 메모리에 읽어 둔 로컬 프로젝트 키 쌍을 사용합니다. 이후 `/api/public/v2/observations?traceId=...`를 조회해 관측 기록 5개와 부모 ID, 완료 시각을 확인하고 입출력 본문이 없는지 검사합니다.

**Langfuse v4의 events_only 배포에서는 기존 `/api/public/traces` 조회를 비활성화합니다.** 해당 경로에서 404가 반환돼도 OTLP 수집 실패를 뜻하지 않습니다. 검사는 지원되는 v2 관측 API를 사용합니다. [공식 API 정의](https://github.com/langfuse/langfuse/blob/6dfeb6049b8fa2795af7bfce1cce9bbe60aca4c0/fern/apis/server/definition/observations.yml)

<a id="stop-resume-and-outage-check"></a>
## 중지·재개와 장애 검사

```sh
# Stop services; preserve all stored data.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env stop
# Resume existing services.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env start
# Inspect status.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env ps
```

통제된 장애 검사에서는 **웹 서비스만** 중지하고 새 `--state`와 `--expect-unavailable`로 검사를 실행한 뒤 `finally` 또는 정리 단계에서 웹 서비스를 다시 시작합니다. 모델 호출 없이 fixture 작업 4건이 완료되고 내보내기 실패가 보고돼야 합니다. 프로토타입은 전송에 실패한 관측 데이터를 재전송하지 않습니다. flush 성공은 exporter가 처리를 끝냈다는 뜻이며 **전달 성공을 보장하지 않습니다**.

설치 중 방화벽·프록시·TLS·정책 오류가 발생하면 중단하고 오류를 보고합니다. 보안 검사를 끄거나 방화벽 설정을 바꾸거나 대체 이미지 미러를 사용하거나 설치를 강행하지 않습니다. 사용자가 명시한 제약입니다.

<a id="boundaries"></a>
## 검증 범위

OTel 관측 모듈은 선택적으로 사용합니다. 큐는 스팬 64개, 배치는 16개로 제한하고 속성 크기에도 제한을 둡니다. HTTP exporter의 제한 시간은 2초이며 실패·손실 건수를 기록합니다. 메모리 내 exporter로 실패와 전송 차단을 포함해 검사합니다. 디스크 임시 저장이나 전달 보장은 구현하지 않았습니다.

이 검사는 Go 내부 스팬 계측, JSONL을 통한 W3C 컨텍스트 전파, 서로 다른 Python 프로세스의 추적 세션 연결, 토큰 비용 추정, 모델 사용량 검증을 수행하지 않습니다. fixture 작업의 토큰이나 가격을 임의로 산정하지 않습니다. 런타임의 실행 책임과 영수증 처리는 그대로 유지하며 관측 실패로 작업을 재시도하거나 워크플로 완료를 결정하지 않습니다.
