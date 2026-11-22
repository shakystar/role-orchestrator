# SMS 워크플로 v1 검증 — 2026-11-19

함께 검증한 runtime revision은 `19e41795ab4ea63005f60c00b92c126e25a79d4f`다. [재현 절차](../runbooks/sms-workflow.md)와 [수용 사례 명세](../../contracts/workflow-metrics-cases.json)를 사용했다.

19개 사례 명세는 `contracts/workflow-metrics-cases.json`에 있다. 고정 소스 조합에서의 최종 결과는 **통과 19, 실패 0, 건너뜀 0**이다. [실행 보고서](../workflow-evaluation.json)에 기록했다. orchestrator `b153741ebf8d43b913b53960dfcce60437d7df5c`와 이 runtime revision에서 추적 파일 변경 없이 검증했다. runtime에 있던 무관한 미추적 연구 문서는 그대로 보존했으며 이 때문에 workspace dirty flag가 표시됐다. 이전 개발 실행도 19/19를 통과했다. projection 복구 검사는 파일시스템 오류를 주입한 뒤 store를 다시 열었다. 그래프 체크포인트·inbox·ack 장애 검사는 Python 하위 프로세스를 실제로 종료했다. Langfuse 격리는 내보내기를 사용하지 않는 독립 워크플로와 exporter 실패·실제 bridge 회귀 검사로 확인했다. 실시간 Langfuse 장애 실험은 수행하지 않았다.

Unix socket 테스트에는 로컬 socket 권한이 필요하다. 처음 sandbox에서 bind를 거부했고 정상적인 로컬 실행 권한으로 같은 테스트를 수행했다. 방화벽·프록시·TLS 설정은 변경하지 않았다. 새 의존성, Docker 이미지, 서비스, 저장소, hosted 리소스를 추가하지 않았다.

<a id="verification-status"></a>
## 검증 상태

구현 중 Go 저장소 전체 race 테스트가 통과했다. 최종 보완 후 영향받은 engine/lifecycle/control 패키지와 장애·용량 테스트도 다시 통과했다. Go vet와 Python 지표 단위 테스트가 통과했고 최종 수용 검사도 19건 모두 통과했다. `b153741`의 hosted 기본 CI가 통과했다. 당시 비공개 저장소 간 hosted 통합은 선택 사항이었으며 로컬 통합 근거는 별도 기록했다. 건너뛴 hosted 작업은 통과로 집계하지 않는다. 이 단계에서는 실제 모델을 호출하지 않았다.
