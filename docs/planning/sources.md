# 후보별 설계 추출 근거

검토일: 2026-11-19. 문서/일부 소스 읽기 수준이며 신규 후보의 설치·벤치마크·통합 검증은 수행하지 않았다. 아래 main 링크는 변경될 수 있다. 구현 채택 시 커밋·버전과 라이선스 원문을 고정한다. 이번에는 코드를 복사하거나 의존성을 추가하지 않았다. 라이선스/상용 기능 경계는 실제 사용할 구성요소별로 다시 확인한다.

## Deep Agents → 위임·컨텍스트·결과물 계약

근거:
- [README](https://github.com/langchain-ai/deepagents/blob/main/README.md): LangGraph 기반 하네스, 분리된 하위 에이전트 컨텍스트, 도구 출력의 파일 보관, 스킬과 교체 가능한 백엔드.
- [subagents.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py): `SubAgent`, `CompiledSubAgent`, `response_format`, isolated/fork 구분, `_FORK_EXCLUDED_STATE_KEYS`, 이름 중복/컨텍스트 모드 검증을 소스에서 확인했다. fork 모드는 실험적이라고 명시한다. 소스 일부를 확인했으며 전체 실행 경로를 감사한 것은 아니다.

추출:
- O-03: 실행을 직접 무제한 생성하지 않고 도구 호출 모양의 작업 제안을 받는다. 이름 대신 버전이 있는 역할 참조와 명시적 산출물 계약을 사용한다.
- O-04: 독립 조사와 인수인계 컨텍스트 정책을 구분한다. 이전 structured response나 임시 요약 상태를 다른 역할의 현재 결과로 오인하지 않게 한다.
- O-05/O-08/O-09: 긴 산출물은 참조로 전달하고 스킬은 필요할 때 로드한다. 반복 흐름은 결과 계약을 가진 하위 워크플로로 노출한다.

예상 이점: 대화 전체 복사와 프롬프트 중복 감소, 후속 작업의 공통 검증 지점 마련. 비용 감소는 아직 미측정이다.

채택 경계: 먼저 설계 패턴을 참고한다. 현재 Codex가 이미 하네스이므로 Deep Agents 전체를 위에 올리면 모델 루프·파일 도구·하위 에이전트 실행을 이중 소유할 수 있다. 직접 사용할 경우 모든 실행이 runtime worker 상한을 따르는지 먼저 확인한다. CompiledSubAgent 연결이 기존 JSONL 어댑터에 곧바로 호환된다는 주장은 하지 않는다.

## CrewAI → 역할과 작업의 책임 명세

근거:
- [README](https://github.com/crewAIInc/crewAI/blob/main/README.md): Crews는 역할 기반 협력, Flows는 이벤트 기반 명시적 제어로 구분한다. 별도 상용 AMP control plane도 소개하므로 이를 모두 OSS 기능으로 간주하지 않는다.
- [task.py](https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/src/crewai/task.py): `Task`의 `expected_output`, `context`, `output_pydantic`, `response_model`, `tools`, `guardrails`, `guardrail_max_retries`와 시작·완료·실패 이벤트를 소스에서 확인했다.

추출: O-02의 TaskContract, 구조 검사와 내용 검토 분리, 한도가 있는 수정 요청. 역할 목표와 작업 완료 조건을 분리해 같은 역할을 다른 프로젝트 작업에 재사용한다. 우리 쪽 재심·근거 요구 스키마는 이 구조에서 도출한 별도 제안이다.

예상 이점: 각 노드의 수동 JSON 검사와 암묵적 프롬프트 규칙을 공통 검증으로 이동할 수 있다. 조건을 충족하지 못한 산출물의 조용한 다음 단계 전파를 줄인다.

채택 경계: CrewAI 전체 실행기를 추가하지 않고 계약 설계를 참고한다. 모델 기반 guardrail을 객관적 진실 검사로 취급하지 않는다. 외부 프레임워크의 자동 재시도를 그대로 가져오면 우리 런타임의 불확실 실행 재시도 금지와 충돌할 수 있다.

## Letta Code → 기억 수정 이력과 역할의 지속성

근거:
- [Letta의 현재 소스 안내](https://github.com/letta-ai/letta/blob/main/README.md): 활성 소스는 letta-code, 기존 V1 API 서버는 archive로 구분한다.
- [Letta Code README](https://github.com/letta-ai/letta-code/blob/main/README.md): memory blocks, MemFS의 Git 기반 컨텍스트 추적, 에이전트/프로젝트 범위 스킬, 메시지 검색, 스케줄을 설명한다. 이 후보는 README 수준이며 구현 보장을 확인하지 않았다.

추출: O-07의 기억 변경 이력·근거·롤백과 O-08의 역할 스킬 분리. 반복 요약으로 오류가 고착될 경우 원래 근거와 이전 버전을 찾을 수 있게 설계한다.

예상 이점: 프로젝트 재개 시 역할의 판단 변화와 정보 출처 추적이 쉬워진다. 전체 대화 재주입과 기억 수정에 따른 회귀를 비교할 수 있다.

채택 경계: 기억·페이징은 P2다. 하네스 자체를 교체하거나 에이전트가 정책을 자유롭게 수정하도록 만들지 않는다. memory blocks나 MemFS가 DB LRU 페이지 구현과 같다는 주장은 하지 않는다. 제품의 ‘identity’ 설명을 실제 자아나 검증된 자율성으로 해석하지 않는다.

## Langfuse → 관측과 평가 데이터셋

근거:
- [README](https://github.com/langfuse/langfuse/blob/main/README.md): OpenTelemetry 기반 관측, LangGraph 통합, 지연·비용 측정, datasets/experiments, 코드 평가·수동 평가·모델 평가를 설명한다.

추출: O-01의 공급자와 분리된 관측 경계, O-06의 고정 사례 기반 비교 실험. SDK 콜백 자동 수집과 별도 Go 프로세스의 수동 계측을 구분한다.

예상 이점: 실행마다 수동으로 로그를 합치는 부담 감소, 정책 버전별 실패·비용 비교. 모델 평가자의 점수만으로 품질 향상을 선언하지 않고 명시적 검사와 사람의 근거 검토를 함께 사용한다.

채택 경계: 라이브러리/OTLP 전송·저장소·호스팅 중 어느 부분을 사용할지 최소 실험에서 결정한다. 관측 저장소는 실행 상태 원본이나 즉각적인 예산 집행 장치로 쓰지 않는다. Langfuse가 Go/Codex 내부를 자동 계측하거나 실제 구독 청구액을 알 수 있다고 가정하지 않는다.

## 기존 후보와 보류

- [Eino](https://github.com/cloudwego/eino): 이전 조사에서 graph-as-tool, callback, interrupt/resume를 확인했다. O-09의 참고 후보이나 Python LangGraph 파일럿과 역할이 겹쳐 새 실행 프레임워크를 추가하지 않는다.
- [Temporal Go SDK](https://github.com/temporalio/sdk-go): 영속 워크플로의 참고 후보. 현재 단일 로컬 실행과 별도 상태 소유권이 먼저이며 분산 서버 운영은 보류한다.
- Microsoft Agent Framework: 이전 README 조회가 404였으므로 이번 추출의 확인된 근거로 사용하지 않았다. AutoGen의 과거 기능을 현행 지원 기능으로 전제하지 않는다.

설계 유사성은 라이브러리 채택 근거의 일부일 뿐이다. 실제 도입은 기존 런타임과의 책임 중복, 라이선스, 고정 버전, 관측 가능한 개선, 롤백 가능성을 확인한 뒤 결정한다.
