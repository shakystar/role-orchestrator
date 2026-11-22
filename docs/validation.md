<a id="integration-validation--2026-11-19"></a>
# 통합 검증 — 2026-11-19

**최소 통합 검사는 통과했으며 정식 채택은 미정이다.**

runtime revision은 [`94bf2e798fc53736babb1b6974f5a84cd7cee00b`](https://github.com/shakystar/role-runtime/commit/94bf2e798fc53736babb1b6974f5a84cd7cee00b)이다. macOS에서 LangGraph 1.2.22, SQLite checkpointer 3.1.3, Python 3.11.9, Go 1.26.11, Codex 0.169.1을 사용했다. Python 의존성은 uv.lock으로 고정했고 CI는 Go 소스 revision을 고정한다. [측정값 JSON](validation-metrics.json)을 참고한다.

<a id="executed-checks"></a>
## 실행한 검사

| 검사 | 결과 |
| --- | --- |
| Python → 실제 Go bridge → 실제 engine.Pool, fixture backend | 통과 |
| 첫 검토 후 LangGraph 체크포인트 저장, 별도 Python/Go 프로세스에서 재개 | fixture와 실제 Codex 모두 통과 |
| 수정 없이 조건부 수락 | 응답을 통제한 테스트에서 통과 |
| 의견이 계속 다르면 1회 수정 후 중단 | 작업 4개 후 deferred로 종료, 통과 |
| 작업 2개 예산을 소진한 뒤 추가 배정 차단 | 통과 |
| 재시작 후 일치하는 Go 완료 영수증 재사용 | fixture와 실제 모델 출력 모두 통과 |
| 사용할 수 있는 Codex 실행 파일 없이 재조회 | 캐시 응답 반환, 새 모델·상태 이벤트 없음. 통과 |
| 작업·역할 충돌, 불확실하거나 손상된 영수증 | Go 테스트에서 거부 |
| Go fmt/vet와 저장소 전체 race 테스트 | 지연 초기화 개선 전 통과. 개선 후 대상 bridge vet/race 통과 |
| Python unittest | 마지막 bridge 변경 후 테스트 4건 통과 |

<a id="live-execution"></a>
## 실모델 실행

실모델 실험 2건을 완료했고 두 실험 모두 합성 근거만 사용했다. 최종 `codex-02`는 최종 runtime revision에서 실행했다. `start`가 제안자와 검토자를 실행한 뒤 중단 지점을 저장했다. 별도 프로세스에서 실행한 `resume`은 수정자와 검토자를 실행하고 종료했다. 영속 성공 영수증 4건, 완료된 adapter 상태 이벤트 4건을 독립적으로 확인했다. 각 CLI 호출은 작업 2개에 Codex 프로세스 하나를 재사용했다. 전체에서 서로 다른 프로세스 ID 2개를 확인했고 동시에 활성화한 워커는 하나였다.

처음 제시한 주장은 여행자에게 가장 충족되지 않은 수요가 숙박 지역 선택이라는 것이었다. 제공한 합성 사례는 이 결론을 뒷받침하지 않아 검토자가 수정을 요청했다. 수정 제안에는 불확실성이 유지됐고 재검토에서 수락됐다. 실모델 backend에는 fixture 응답을 제공하거나 결정을 강제하지 않았다. 최초 주장·근거·역할 지침은 이 통합 시험을 위해 설계했으므로 **일반적인 자율 연구 품질이나 실제 관광 수요를 입증하지 않는다.**

최종 실행의 모든 작업은 설정한 heartbeat 간격 15초보다 짧았다. 상태 이벤트를 관측했으며 긴 턴의 heartbeat 복구는 이번에 다시 검증하지 않았다. 웹 검색, 참여자 모집, 유료 캠페인, 외부 연락, 제품 배포는 수행하지 않았다.

<a id="failure-found-and-corrected"></a>
## 발견한 오류와 수정

첫 실모델 그래프 `codex-01`은 작업 4개를 모두 완료했다. 이후 제한된 환경에서 캐시 영수증을 조회할 때 bridge가 영수증 확인 전에 Codex를 초기화하여 `codex RPC stdout closed`로 실패했다. backend를 지연 초기화하도록 수정하고 캐시 적중 시 초기화를 방지하는 Go 회귀 테스트를 추가했다. 최종 검증에서 실행 파일을 `/nonexistent/codex`로 지정해도 실제 모델 출력을 읽었다. 최초 조회 실패는 실패로 보존했으며 변경 후 전체 실모델 그래프를 `codex-02`로 다시 실행했다.

<a id="limits-and-next-decision"></a>
## 검증 한계와 후속 결정

이 파일럿은 역할 3개, 워커 1개, 수정 1회, 로컬 SQLite 체크포인트와 순차 bridge를 사용했다. 범용 동적 계획, 동시 스케줄링, SMS 기반 그래프 재개, 불확실한 외부 효과의 장애 복구, 비용 추정, 연구 방법론, memory paging은 검증하지 않았다. Go 작업 영수증과 Python 체크포인트는 별도 저장소이며 외부 효과의 exactly-once를 보장하지 않는다.

이 범위에 합의한 뒤 정식 기획 여부를 결정한다. 후속 설계 대상은 버전이 있는 작업·이벤트 계약, SMS에서 그래프로의 재개 신호, 근거·결정 기록, 예산 정책이다. 이 실험에서는 구현하지 않았다.

<a id="hosted-ci-scope"></a>
## Hosted CI 범위

최초 [CI 실행](https://github.com/shakystar/role-orchestrator/actions/runs/44457505973)은 테스트 시작 전 role-runtime checkout 단계에서 실패했다. 당시 두 저장소는 비공개였고 role-orchestrator의 기본 Actions 토큰으로 role-runtime을 읽을 수 없었다. 해당 검증 중에는 저장소 공개 여부와 계정 인증 정보를 변경하지 않았다.

이 단계의 CI는 Python 그래프 테스트 3개를 항상 실행한다. 실제 Go bridge 통합 작업을 활성화하려면 role-runtime 읽기 권한만 가진 `ROLE_RUNTIME_READ_TOKEN` secret과 저장소 변수 `ROLE_RUNTIME_INTEGRATION=true`를 설정한다. 미설정 시 작업은 건너뛰며 통과로 집계하지 않는다. 실제 bridge를 포함한 테스트 4건은 모두 로컬에서 실행했다. hosted 실모델 Codex 실행은 설정하지 않았다.
