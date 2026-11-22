# 의존성과 재사용 범위

버전은 `uv.lock`에 고정하며 `uv sync --frozen`으로 설치합니다.

| 구성요소 | 고정된 직접 의존성 버전 | 라이선스 | 사용 범위와 대안 |
| --- | --- | --- | --- |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 1.2.22 | MIT | StateGraph, 조건부 분기, 중단·재개에 사용합니다. 대안으로 소규모 명시적 상태 머신이나 Go Eino가 있으며 성능 비교는 수행하지 않았습니다. |
| [SQLite 체크포인터](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite) | 3.1.3 | MIT | 그래프 체크포인트를 로컬에 영구 저장합니다. 다른 LangGraph 체크포인트 백엔드로 대체할 수 있습니다. |
| [role-runtime](https://github.com/shakystar/role-runtime) | 검증 기록의 런타임 리비전 참조 | 해당 저장소의 조건 | 외부 실행 파일로 사용합니다. Go 구현을 Python으로 복사하지 않습니다. |
| [codex-adapter](https://github.com/shakystar/codex-adapter) | role-runtime에서 고정 | MIT | Go가 Codex app-server에 접근할 때 사용하는 간접 의존성입니다. |

LangGraph는 포크 없이 설치된 라이브러리로 사용합니다. SQLite는 Python 표준 라이브러리에 포함되며 체크포인터는 별도 의존성을 추가로 설치합니다. 간접 의존성은 잠금 파일에 기록합니다. 이 프로젝트는 LangSmith 호스팅 추적을 요구하거나 설정하지 않습니다. Python 직접 의존성은 설치된 패키지의 메타데이터를 확인했습니다. Codex 자체는 재배포하지 않습니다.

## 선택적 로컬 관측 검사

OpenTelemetry Python SDK와 OTLP/HTTP exporter **1.45.2**를 포크 없이 사용해 배치 처리와 전송을 수행합니다. 설치된 패키지 메타데이터에서 Apache-2.0 라이선스를 확인했습니다. 대안인 Langfuse SDK와의 비교는 수행하지 않았습니다. Docker Compose 구성은 Langfuse 소스 커밋 `6dfeb6049b8fa2795af7bfce1cce9bbe60aca4c0`를 바탕으로 수정했으며 이미지와 다이제스트는 infra/langfuse에 기록했습니다. Langfuse와 지원 서비스는 각각 별도의 공식 컨테이너로 실행하며 해당 코드를 이 저장소로 복사하지 않습니다. 각 구성요소에는 원 배포처의 라이선스가 적용됩니다. [Langfuse](https://github.com/langfuse/langfuse/blob/main/LICENSE), [PostgreSQL](https://www.postgresql.org/about/licence/), [Redis](https://github.com/redis/redis/blob/7.4/COPYING), [ClickHouse](https://github.com/ClickHouse/ClickHouse/blob/master/LICENSE), [MinIO](https://github.com/minio/minio/blob/master/LICENSE)의 고지를 참조합니다. Python 프로젝트의 MIT 라이선스가 모든 이미지에 적용되지는 않으며 Redis 7.4에는 별도 라이선스 조건이 있습니다. 파일럿에서는 기업용 기능이나 호스팅 모델 게이트웨이를 사용하지 않습니다.

브리지 v1 클라이언트는 동일한 Python OTel 버전을 사용합니다. 함께 사용하는 Go 브리지는 OTel 1.47.0(Apache-2.0)을 고정합니다. 세부 사항은 role-runtime의 THIRD_PARTY.md와 go.sum에 있습니다. 공유 요청 벡터 파일은 프로젝트에서 직접 작성했으며 상위 프로젝트로부터 복사하지 않았습니다.
