<a id="scope"></a>
# 작업 범위

- 최소 통합 실험입니다. 변경 전 README.md와 docs/validation.md를 읽습니다.
- 명시된 과제에서 요구하는 경우에만 운영 오케스트레이션이나 사업 조사로 범위를 넓힙니다.
- 의사결정·체크포인트 상태는 Python에서, 실행·세션 영수증은 role-runtime에서 관리합니다.
- Python에서 Codex를 직접 호출하거나 Go 런타임 내부 구현을 이 저장소로 복사하지 않습니다.
- fixture와 실제 모델의 검증 근거를 구분합니다. 합성 근거를 시장 검증 결과로 보고하지 않습니다.
- 재시도·그래프 반복·작업 제출에 상한을 둡니다. 결과가 불확실한 외부 작업을 묵시적으로 재실행하지 않습니다.
- 산출물·모델 대화 기록·인증 정보·로컬 세션을 Git에서 제외합니다.
- uv.lock으로 의존성 버전을 고정합니다. 프로토콜을 변경하면 실제 Go 브리지로 검사합니다.

<a id="user-facing-completion-reports"></a>
# 사용자 완료 보고

- [완료 보고 규격 v1](docs/completion-reporting.md)을 따릅니다.
- 구체적인 변경 전후 동작과 사용자가 새로 할 수 있는 일을 먼저 쓰고, 이를 구현한 구성요소를 바로 뒤에 제시합니다. 두 내용을 모두 포함합니다.
- 구현, 통합, 합성 검증, 실제 모델·운영 검증을 구분합니다. 기존 상태를 과장하거나 측정하지 않은 개선을 주장하지 않습니다.
- 검증 결과와 남은 범위, 근거 링크를 간결하게 제시하며 마칩니다. 작업 규모에 맞게 형식을 조정하고 파일 목록이나 검사 건수만으로 설명을 대신하지 않습니다.


## 개발 기준과 검증

[공통 개발 정책](https://github.com/shakystar/agent-collaboration-lab/blob/c541f5ba978d06bc0e5ce7a1c1df0dff1166149f/docs/development-policy.md)을 적용한다. 검증 명령은 레포 루트에서 실행한다. Python 3.11 이상과 uv 환경에서 `uv run --frozen python -m unittest discover -s tests -v`
