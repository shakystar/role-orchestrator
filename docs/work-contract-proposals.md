# 후속 작업 제안 기록

협업 문서 워크플로에서 이 옵션을 선택하면 두 역할을 시작하기 전에 주어진 작업 계약을 읽는다. 호출자가 목표·필수 항목·관측 자료를 지정하면 작성자가 초안을 작성한다. 검토자는 관측 자료를 인용하고 판단 근거를 설명하며 complete, revise, investigate, defer 중 하나를 선택한다. complete 이외의 조치는 결정을 바꾸거나 확정할 수 있는 확인 절차나 조건도 명시해야 한다. 호스트가 검토 결과를 최종 산출물로 게시한다. 이 조치를 받아 후속 작업을 배정하는 dispatcher는 없다.

함께 사용할 runtime revision은 40a3baf4d9a195d565e6ecf9f1532b476c230425다. role-runtime에서 runtime-workflow를 빌드한 뒤 orchestrator 루트에서 실행한다.

    ../role-runtime/bin/runtime-workflow --state /tmp/proposal-demo --run-id proposal-demo --backend codex --workers 1 --contract examples/discovery-contract.json

다른 터미널에서 완료될 때까지 poll한다. 매번 새 Python 프로세스에서 재개할 수 있다.

    PYTHONPATH=. .venv/bin/python scripts/check_model_workflow.py --runtime-state /tmp/proposal-demo --state artifacts/proposal-demo --run-id proposal-demo

--verify를 추가해 다시 실행하면 완료 작업 2개를 확인하고 영수증에 연결된 인계 자료를 내보내며 기록된 제안을 표시한다. runtime과 그래프 상태는 함께 보관한다. 기존 상태를 다시 열면 원래 계약에 연결되므로 입력을 바꾸려면 새 상태를 사용한다.

예제는 관심사가 혼재된 여행 인터뷰를 합성한 사례다. 제한된 근거에서 팀이 어떻게 추론하는지 확인하며 실제 여행 수요는 검증하지 않는다. evidence ID가 허용 목록에 있고 중복되지 않는지 검사한다. 참조 무결성만으로 추론의 타당성을 입증할 수는 없다.

해시로 연결된 작성자 산출물에는 호스트가 제공한 원래 타입의 task_contract가 그대로 담긴다. agent-eval은 기존 saved-output CLI로 이 산출물과 기록된 제안을 받는다. 선택적 의미 평가에서 이 형태의 산출물에는 proposal-v1을 사용한다. 근거와 목표에서 제안 이유 및 결정 확인 절차를 도출할 수 있는지 평가한다. 결정은 검토자가 내리고 독립 evaluator가 이후 기록된 결정을 채점한다.

    uv run --project evaluation/m1 --frozen python scripts/evaluate_handoff.py artifacts/proposal-demo/handoff-evidence --semantic-model codex-adapter/gpt-6.2-sol

기존 evaluator 실행 문서에 따라 AGENT_EVAL_CODEX_GRADER를 지정한다. --requirements로 검토 결과가 필수 항목을 포함했는지도 평가할 수 있다. 이 옵션을 쓸 때는 검토자가 설명해야 할 사항을 계약에 적는다.

검증에서 주입 모델은 두 역할 모두에서 계약을 받았고 조사를 제안했다. 후속 메시지나 대기 없이 완료된 검토 영수증이 생성됐다. 계약 테스트에서는 네 가지 조치를 모두 실행했고 알 수 없는 근거, 중복 인용, 변경된 문서 해시, 다음 확인 절차 누락을 거부했다. 영향받은 Go 패키지의 vet/race 검사가 통과했다. 별도 agent-eval CLI smoke 테스트에서는 합성 grader에 제안 rubric, 관측 자료, 산출물이 전달되는지 확인했다. 이 검사는 연결 상태만 측정했다.

실제 문서 생성을 두 차례 시도했으나 작성자 턴 전에 account_read / timeout / not_started로 종료됐다. 중간 doctor 검사는 성공했으나 마지막 검사는 workspace_routing_timeout을 반환했다. 이 두 시도에서 실제 모델 제안이나 제안 품질 점수는 얻지 못했다.

<a id="live-retry-with-temporary-address-mapping"></a>
## 임시 주소 매핑을 사용한 실모델 재시도

2026-11-20 호스트가 chatgpt.com의 IPv4 주소 두 개를 조회했다. 정상 호스트명과 인증서 검증을 사용한 HTTPS에서 104.18.32.47은 200(TCP 연결 0.035초)을 반환했고 172.64.155.209는 연결 중 timeout(5초 제한)이 발생했다. 이 주소는 해당 실행 시점의 관측값이므로 다른 실행에 임시 매핑을 적용하려면 다시 조회·확인해야 한다.

새 Linux/amd64 컨테이너에서 Docker --add-host로 chatgpt.com만 응답한 IP에 연결했다. 컨테이너에는 해당 주소 하나만 표시됐고 공식 Codex doctor는 routingAvailable=true를 반환했다. pinned-contract-01에서 워커 하나로 작성과 검토를 실행했다. 논리 작업 2개, 시도 2회, SMS 승인 1회, 시작 재시도 0회였다. 검토자는 complete를 제안했다. 문서는 관측한 활동 계획의 어려움과 숙소에 대한 가설을 구분했고, 초점을 바꿀 근거를 얻을 수 있는 독립 인터뷰를 제시했다. 검토자는 obs-activity와 obs-stay를 인용했다. complete는 파일럿 문서의 완료를 뜻하며 관측 대상은 합성 집단이다.

첫 독립 채점에서는 M3 이미지의 기본 Python 작업 디렉터리가 마운트한 M2 패키지보다 먼저 검색됐다. 해당 점수는 별도로 보존했다. 후속 채점에서는 작업 디렉터리를 /m2로 설정하고 실제 로드한 패키지 경로를 확인한 뒤 같은 인계 자료를 채점했다. Codex 인증은 읽기 전용으로 마운트했고 컨테이너는 --rm을 사용했다. 호스트 DNS나 방화벽은 변경하지 않았다. 실패가 발생한 네트워크 구간은 특정하지 못했다.

고정한 M2(89695e7), 평가 eval-b7a8b372b253430590e81e4a9f709482의 결과는 source-hash met, proposal-v1 met였으며 scorer 오류는 없었다. 합성 근거로 생성한 실모델 제안 1건이며 사람 기준 판정은 등록하지 않았다.
