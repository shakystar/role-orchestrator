# role-orchestrator 문서 색인

[저장소 README](../README.md)에 기본 CLI의 설치와 첫 실행 방법이 있다. 문서를 추가하거나 수정할 때는 실험실 비공개 저장소의 [공통 문서 작성 정책](https://github.com/shakystar/agent-collaboration-lab/blob/main/docs/documentation-policy.md)과 [마일스톤 템플릿](https://github.com/shakystar/agent-collaboration-lab/blob/main/templates/milestone.md)을 사용한다. 두 링크는 해당 저장소의 읽기 권한이 필요하다.

## 설계와 계약

| 확인할 내용 | 문서 |
| --- | --- |
| Python·Go 간 요청 형식, 식별자 수명, 상태 소유권, 장애 처리 | [Bridge v1 계약](protocol.md) |
| SMS 수신, 체크포인트·ack 순서, 지표와 모듈 책임 | [SMS 워크플로 구현](workflow-implementation.md) · [v1 기준 설계](planning/workflow-metrics-v1.md) |
| 관측 범위, trace 전파 설계와 참고 근거 | [관측 설계](planning/observability.md) · [참고 자료](planning/sources.md) |
| 작업 계약과 후속 조치의 기록 범위 | [후속 작업 제안](work-contract-proposals.md) |
| 개발 역할 배정·산출물 수락·중단 후 조회 | [v0.1.1 개발 인계 계약](features/development-handoff.md) |
| 구현 후보와 후속 범위 | [기회 검토](planning/opportunities.md) |

## 실행과 검증

| 목적 | 실행 절차와 근거 |
| --- | --- |
| 기본 그래프 중단·재개와 캐시 재조회 | [기본 CLI](../README.md) · [통합 검증](validation.md) · [측정값](validation-metrics.json) |
| SMS 기반 합성 인계와 수용 검사 재현 | [실행 절차](runbooks/sms-workflow.md) · [초기 장애 실험](workflow-feasibility.md) · [v1 검증](validation/sms-workflow-v1.md) · [수용 검사 결과](workflow-evaluation.json) |
| 실제 Codex 작성·검토와 재시작 확인 | [실모델 검증](model-workflow-validation.md) · [결과 JSON](model-workflow-validation.json) |
| 로컬 Langfuse 연결과 trace 확인 | [Docker 실행 절차](../infra/langfuse/README.md) · [로컬 수집 검증](langfuse-validation.md) · [Bridge trace 검증](contract-trace-validation.md) |
| 저장된 인계 평가와 대조군 판정 | [M1 인계 및 평가 절차](m1-handoff-check.md) · [M2 grounding 실험](m2-grounding-probe.md) |
| 사용자에게 구현·검증 결과 보고 | [완료 보고 기준](completion-reporting.md) |

## 보완할 문서

- **네 저장소 기본 브랜치의 통합 검증 기록**은 [agent-eval 이슈 #1](https://github.com/shakystar/agent-eval/issues/1)에서 추적한다. 해당 조합의 실행이 끝나면 사용한 네 커밋, 실제 결과, 재현 명령과 증거 링크를 연결한다.
- **불확실한 실행의 복구 절차**는 현재 [Bridge 계약](protocol.md#state-ownership-and-crash-boundary)에 재전달 차단 조건까지 기록돼 있다. 운영자가 결과를 대사하고 재개할 수 있는 절차는 복구 방식을 확정한 뒤 문서화한다.


[승인된 수정 제안 기능 명세](features/approved-revision.md)

[독립 리뷰팀 호출과 검증 기록](features/independent-review.md)
