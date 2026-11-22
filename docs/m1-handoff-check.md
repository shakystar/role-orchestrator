# 저장된 검토 결과의 해시 보정 (2026-11-20)

임시 runtime 디렉터리를 제거한 뒤 저장된 모델 출력에서 model-1119a의 작성 문서와 검토 결과를 복원했다. 두 파일 모두 기존 재시작 기준 SHA-256과 일치했다. 기준 샘플에는 로컬 지표 원장의 완료된 검토 attempt ID가 있다. 두 번째 샘플은 source_sha256의 16진수 한 자리를 바꾸고 별도의 합성 ID를 부여한 오프라인 대조군이다.

agent-eval M1의 공개 CLI(16b96ee28b58a92c463d157b421ed2c13becedf9)로 saved-output-v1을 통해 두 출력을 가져왔다. Inspect AI(ec5d110b)의 mock provider로 /source_sha256과 작성 파일 해시를 비교했다. 최초 평가와 부모 평가를 연결한 반복 평가 모두 기준 샘플 met, 대조군 unmet이었다. 판정 가능한 샘플은 2개, scorer 오류는 0건이었다. 두 번째 가져오기는 duplicate를 반환했다. runtime이나 Codex 턴은 실행하지 않았다.

M1이 고정한 Inspect 의존성이 설치된 Python 3.12 환경에서 재현한다. 변경되지 않는 M1 소스 checkout, 저장된 근거 디렉터리(document.json, review.json, restart-baseline.json, metrics.sqlite), 새 출력 디렉터리를 사용한다.

    python scripts/check_m1_handoff.py M1_SOURCE SAVED_EVIDENCE NEW_WORK_DIR

작업 디렉터리에는 비공개 입력, 로컬 저장소, JSON·Markdown 보고서가 생성된다. 이 검사는 참조 무결성만 확인한다. 두 샘플은 실제 모델 검토 결과 하나와 이를 오프라인에서 변형한 결과 하나다.

<a id="operational-handoff-to-m1"></a>
## M1으로 인계

모델 워크플로 검사에서 `--verify`를 지정하면 그래프 상태의 `handoff-evidence/`에 두 산출물, 영수증 해시, attempt ID를 저장한다. 디렉터리 이름을 바꿔 완성된 스냅샷을 게시한다. 동일한 스냅샷은 재사용할 수 있지만 바이트나 attempt 식별자가 다르면 기존 스냅샷을 덮어쓸 수 없다. 검증 JSON은 `review_matches_document` 대신 `handoff_evidence`를 반환한다. 실행 시 전제 조건은 Go에서 계속 확인한다. Inspect가 없어도 그래프 완료와 모델 tracing 검사는 실행된다.

role-orchestrator 루트에서 전용 Python 3.12 uv 프로젝트를 사용한다. 처음 실행하면 평가 환경을 동기화하고 scorer를 실행한다.

    uv run --project evaluation/m1 --frozen python scripts/evaluate_handoff.py path/to/graph-state/handoff-evidence

별도 설치 명령은 `uv sync --project evaluation/m1 --frozen`이다. 최초 설정에는 고정한 저장소에 대한 Git 접근과 패키지 인덱스 접근이 필요하다. 평가 전용 lock은 Inspect extra를 포함한 agent-eval과 전이 의존성을 고정한다. 새 evaluator revision을 채택할 때 lock을 명시적으로 갱신한다.

agent-eval 커밋 16b96ee28b58a92c463d157b421ed2c13becedf9에서 검증했다. 공개 CLI와 버전 1 manifest만 사용한다. 실행마다 스냅샷 옆 `handoff-evaluations/<evaluation_id>/`에 별도의 비공개 저장소와 보고서를 생성한다. result.json은 run, 검토 attempt, evaluation, criterion, 출력 digest와 보고서를 연결한다. 완료 보고서가 잠정 판정을 제공한다. 규칙 미충족 시 결과와 0이 아닌 종료 코드를 반환하고, CLI 또는 근거 오류 시 결과를 게시하지 않고 오류를 발생시킨다. 실행 상태는 runtime 완료 영수증으로 판단한다. 토큰과 비용은 추정하지 않는다. 같은 저장소에서 부모 평가와 연결해 재채점하려면 위의 최초 보정 절차를 사용한다.

이관 검증에서는 보관한 model-1119a 바이트와 지표 원장의 완료 attempt로 스냅샷 내보내기 테스트에 필요한 영수증 필드를 복원했다. 원본은 met, 한 글자를 바꾼 결과는 unmet으로 기존 비교와 일치했다. 두 명령 모두 모델을 실행하지 않았다. 스냅샷 내보내기와 실제 M1 CLI 채점을 오프라인에서 검증했다.

2026-11-20 Intel macOS, CPython 3.12.13, uv 0.11.13 환경에서 Git 설치를 확인했다. 새 평가 가상환경에 frozen sync로 패키지 81개를 설치했다. 설치된 VCS 메타데이터가 lock의 agent-eval·Inspect 커밋과 일치했다. PYTHONPATH를 해제하고 보관한 원본·변형 스냅샷을 frozen offline uv로 실행한 결과는 각각 met(종료 0), unmet(종료 1)이었다.

<a id="optional-semantic-grading"></a>
## 선택적 의미 평가

현재 평가 lock은 M2 커밋 89695e754a077cb8ed01db6cdd26ebd1d67826cf를 사용한다. 기존 evaluation/m1 프로젝트 경로와 기본 해시 전용 명령은 호환된다. 실모델 의미 평가는 고정한 agent-eval checkout에서 M2 codex-grader를 빌드한다(`cd adapters/codex && go build -o /absolute/path/to/codex-grader .`). 로컬 Codex 인증 후 role-orchestrator에서 실행한다.

    export AGENT_EVAL_CODEX_GRADER=/absolute/path/to/codex-grader
    uv run --project evaluation/m1 --frozen python scripts/evaluate_handoff.py path/to/handoff-evidence --semantic-model codex-adapter/gpt-6.2-sol

플래그를 활성화하면 선택한 grader에 문서, rubric, 저장된 검토 결과가 전달된다. result.json은 해시 판정을 유지하고 semantic 객체에 evaluation ID, 판정, 처리 상태, 가능한 경우 구조화된 실패 정보와 생성된 M2 신뢰도 보고서 경로를 담는다. 채점된 unknown은 근거 부족, scorer_error는 평가 실패다. 명령이 완료되면 종료 코드 0은 요청한 검사 모두 충족, 1은 미충족 존재, 2는 하나 이상 unknown을 의미한다. 후속 작업을 제출하지 않는다.

이 명령의 종단 검증에서 보관 원본은 met/met(종료 0), 같은 해시에 반대 근거를 넣은 샘플은 met/unmet(종료 1)이었다. 배정 전 계정 timeout을 주입하면 not_started 실패가 보존되고 met/unknown(종료 2)이었다. 변형 해시는 기본 모드에서 unmet이었다.

<a id="independent-reviewer-preparation"></a>
## 독립 검토 준비

evaluation/grounding/blind-cases.json과 rubric-v1.txt를 함께 사용한다. human-review.csv를 비공개 저장소에 복사하고 각 사례에 검토자, met/unmet/unknown, 관련 원문 구절 또는 부족한 근거, 판단 이유를 기록한다. 모델 응답을 열람하거나 비교하기 전에 작성한 표를 고정한다. 합성 사례 5개는 채점 전 준비 자료다. 사람 기준 판정과 일치도 통계는 아직 없다. 이후 비교 시 고정한 라벨을 정확한 사례·원문·rubric 버전에 연결한다.

<a id="faithfulness-and-completeness"></a>
## 원문 충실도와 요구사항 충족도

두 축을 평가하려면 미리 정한 요청 및 필수 항목을 전달한다.

    uv run --project evaluation/m1 --frozen python scripts/evaluate_handoff.py path/to/handoff-evidence --semantic-model codex-adapter/gpt-6.2-sol --requirements evaluation/completeness/weekday-weekend.json

requirements 계약은 버전 1 JSON 객체다. 비어 있지 않은 요청과 서로 다른 필수 항목 1~8개를 포함한다. 답변을 판정하기 전에 항목을 정한다. 이 플래그가 없으면 기존 grounding 전용 및 해시 전용 모드의 의미가 유지된다. 보고한 해시는 비공개 평가 디렉터리에 직렬화해 저장한 requirements 사본을 식별한다.

agent-eval CLI는 해시를 채점한 뒤 문서와 기존 grounding rubric으로 충실도를 판정한다. 충족도 평가에는 동일한 문서·답변과 함께 요청 및 필수 항목 하나를 담은 개별 rubric을 전달한다. 각 결과는 해당 평가의 agent-eval 신뢰도 보고서에서 가져온다. 필수 항목 하나라도 unmet이면 전체가 미충족이다. unmet이 없고 unknown이 있으면 전체는 unknown이며, 모든 항목이 met이어야 전체 충족이다. 항목이 N개면 의미 평가는 N+1회다. 각 평가에는 고유 ID와 오류 상태가 있다. 다른 항목에서 미충족이 확정돼도 unknown이 하나라도 있으면 CLI는 종료 코드 2를 반환한다.

result.json의 semantic은 **충실도** 판정이다. completeness에는 요청, 요청 digest, 종합 판정, 각 필수 항목의 문구·판정·보고서 참조가 있다. 문제가 된 항목은 저장된 문서·검토 결과와 함께 확인한다. 고정한 공개 신뢰도 CLI는 잠정 판정, 처리 및 실패 상세를 제공하지만 grader의 자유 서술 이유는 내보내지 않는다. orchestrator도 이유를 생성하지 않는다. 이유 원문 표시가 필요해지면 agent-eval에 선택적 로컬 상세 필드나 CLI 명령이 필요하다.

합성 g04는 주중·주말 규칙을 요청하지만 주말 규칙만 인용한다. 실제 Codex 실행에서 해시와 충실도는 met, 주중 항목은 unmet, 주말 항목은 met여서 종합 판정은 미충족이었다. 위와 같이 AGENT_EVAL_CODEX_GRADER를 설정한 뒤 재현한다.

    uv run --project evaluation/m1 --frozen python evaluation/completeness/check_g04.py artifacts/g04-fresh --semantic-model codex-adapter/gpt-6.2-sol

검사 스크립트는 미리 선언한 예상 결과를 확인한다. 채점은 모두 agent-eval이 수행한다. 두 축의 독립 검토에는 evaluation/completeness/human-review.csv를 사용한다. 기존 단일 축 사람 라벨은 충족도 판정을 제공하지 않는다.

같은 g04 문서·검토 결과에 주말 항목만 요청하면 충실도와 충족도가 모두 met(종료 0)이다. 두 항목을 요청하면 주중 규칙 누락으로 종료 코드 1이다. fixture grader로 호출 전 계정 timeout을 주입하면 충실도와 두 필수 항목이 모두 unknown(종료 2)이다.
