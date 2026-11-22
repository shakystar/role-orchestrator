# M2 인계 결과의 근거 검증 — 2026-11-20

임시 Python 3.12 평가 환경에서 agent-eval 커밋 89695e754a077cb8ed01db6cdd26ebd1d67826cf, Go codex-grader와 모델 codex-adapter/gpt-6.2-sol을 사용했다. 운영용 M1 의존성은 기존 revision으로 고정했다.

실제 검토 결과와 구성한 대조군은 동일한 저장 문서를 인용한다. 대조군에서 판단 이유만 바꿔, 원문이 명시적으로 금지한 ‘불확실한 작업을 대사 없이 자동 재시도한다’는 내용을 원문에 귀속시켰다. 사전 기대 판정은 각각 met와 unmet이다. 기술 검사용 대조군이며 사람이 내린 판정은 등록하지 않았다.

두 출력 모두 M1 source-hash 기준을 충족했다. M2에 출력 두 건, 과제와 grounding rubric을 등록했다. grounding-live-01에서 배정 오류 2건이 발생했고 둘 다 account_read / timeout / not_started였다(항목별 초기화 시도 2회). 의미 판정은 0건이었다. 모델을 호출하지 않는 별도 doctor 검사는 workspace_routing_timeout을 반환했다. 신뢰도 보고서는 비교 가능한 쌍 0개, unverified였다. 이 실행으로 의미 평가 정확도를 추정할 수 없다.

비공개 manifest, 기대 결과, 보고서와 조회 가능한 store는 artifacts/m2-check-89695e7/handoff-grounding에 있다. 계정 사전 확인을 통과하면 같은 store와 고정 입력을 유지하고 새 evaluation ID로 실행한다. 실패한 평가는 비교용으로 보존한다.

<a id="retry-after-account-recovery"></a>
## 계정 연결 복구 후 재시도

grader가 사용하는 /usr/local/bin/codex의 doctor 검사에서 routingAvailable=true가 반환됐다. 새 평가 grounding-live-02는 같은 store·입력·rubric·모델을 사용했다. 두 항목 모두 오류 없이 채점했다. 원본 검토는 met, 모순되는 이유를 넣은 대조군은 unmet으로 사전에 정한 두 결과와 일치했다. grounding-live-01의 unknown 판정 2개는 보존했다. 방화벽 설정은 변경하지 않았다. 이 관측으로 이전 timeout의 원인을 특정할 수 없다. 신뢰도 보고서는 사람 기준과의 비교 0건으로 여전히 unverified다.
