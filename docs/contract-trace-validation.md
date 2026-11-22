# Bridge v1과 trace 전파 수용 검사 — 2026-11-19

구현과 합성 수용 검사, **실모델 수용 검사 1건**을 완료했다. 첫 실모델 시도는 계정 사전 검사에서 `workspace_routing_timeout`으로 실패하여 모델 턴을 시작하지 못했다. 이후 모델을 호출하지 않는 doctor가 라우팅 가능을 보고했고 새 실험에서 실모델 검사를 통과했다. 이 결과는 명시한 기술 범위에 한정하며 orchestrator 정식 채택이나 업무 효과 평가는 별도다.

<a id="implemented-boundary"></a>
## 구현 범위

- 요청·응답 형식의 버전을 엄격히 검사한다. Python과 Go는 동일한 요청 벡터를 독립적으로 검증한다.
- run/trace 식별자를 영속화하고 task/attempt 연결을 불변으로 유지한다. 명시적으로 선택한 legacy 모드는 격리한다.
- Python subprocess 전송에서 Go 요청·실행 span까지 W3C parent 정보를 전달한다.
- 재개 시 같은 trace와 새 span ID를 사용한다. 캐시 재조회는 실행 span을 생성하지 않는다.
- 실패·불확실한 영수증을 자동으로 재시도하지 않는다. 관측 실패로 대체 작업을 실행할 수 없다.
- 선택적으로 loopback Langfuse에 전송한다. 메타데이터 전용 로컬 로그와 SDK 큐 모두 한도를 둔다.

함께 사용한 runtime revision은 `47eb03531097e925145682600d30cb96c1ac6f8e`이며 선택적 hosted 통합 작업도 이 커밋을 고정한다.

<a id="evidence"></a>
## 검증 근거

| 검사 | 결과 |
| --- | --- |
| 빌드한 Go 바이너리를 사용한 Python unittest discovery | 하위 프로세스 재개·캐시·exporter 실패를 포함해 12건 통과 |
| Go vet와 race 테스트 | 통과. 초기화 정리 수정 후 bridge 대상 검사를 다시 실행 |
| 공통 통신 벡터 | 버전·타입·중복·알 수 없는 키·대소문자를 구분하는 키, W3C flags `03`을 포함해 Python과 Go가 일치 |
| fixture 그래프 시작 → 별도 프로세스 재개 → 캐시 재조회 | fixture 실제 실행 4회, 전달 5회, 관측 17건 저장. 모든 ID와 부모 관계가 로컬 기록과 일치 |
| trace payload 제외 | fixture 관측 17건에서 입력·출력 payload 없음 |
| loopback exporter 연결 실패 | 작업 성공. Python과 Go가 각각 전송 실패 2건 기록. 정리까지 1.495초, 모델 호출 0회 |
| 최초 실모델 시도 | 계정 사전 검사 실패(`workspace_routing_timeout`), 모델 턴 시작 0회 |
| 실모델 실패 trace 조회 | 관측 4건이 모두 저장됨. 정확한 부모 관계, ERROR 상태, 입력·출력 payload 부재 확인 |
| 모델을 호출하지 않는 공식 doctor | 처음에는 라우팅 오류를 재현했고 이후 검사에서는 라우팅 가능을 보고. 호출 제한이나 잘못된 로컬 경로를 원인으로 확인한 근거 없음 |
| 새 실모델 수용 검사 | 모델 작업 1건 완료. `/nonexistent/codex`로 캐시 재조회한 것을 포함해 요청 2회, 관측 7건 저장. 모든 ID·부모 관계가 일치하고 입력·출력 payload 없음 |

실모델 실패 경로에서 기존 지연 초기화 버그도 발견했다. 생성자가 오류를 반환했는데 typed-nil 어댑터를 저장해 close에서 panic이 발생했다. bridge가 생성에 성공한 executor만 저장하도록 수정했다. 회귀 테스트는 typed nil과 오류를 함께 반환하고 안전하게 close되는지 확인한다. 이 수정으로 상위 라우팅 문제가 해결됐다고 판단하지 않는다.

Go OTel 1.38.0이 Python OTel 1.45.2의 유효한 W3C flags `03`을 거부해 Go OTel을 1.47.0으로 올리고 공통 회귀 벡터에 추가했다. 처음에는 sandbox DNS 제한으로 의존성 다운로드가 실패했지만 정상 실행 승인 후 표준 소스에서 받았다. 방화벽·프록시·인증서·mirror 설정은 변경하지 않았다.

<a id="reproduce"></a>
## 재현

```sh
# Build ../role-runtime/bin/runtime-bridge first.
ROLE_RUNTIME_BIN=../role-runtime/bin/runtime-bridge .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_contract_trace.py \
  --state artifacts/contract-trace-fixture-new \
  --runtime ../role-runtime/bin/runtime-bridge
```

로컬 Langfuse 실행 문서에서 Git 추적 대상이 아닌 환경 파일을 준비한다. 라우팅이 가능할 때 `--live --codex /absolute/path/to/codex`를 추가해 모델 작업 1개와 캐시 재조회를 실행한다. 새 실험은 항상 새 상태에서 시작하고, 강제 재실행 목적으로 실패 영수증을 삭제하지 않는다. 실모델 수용 검사 스크립트에는 모델 재시도 루프가 없다. 관측 API만 수집 결과가 나타날 때까지 poll한다.

민감한 정보를 제거한 측정값은 `contract-trace-metrics.json`에 있다. 프롬프트, 영수증, 세션 식별자, 상태 로그와 인증 정보는 Git에서 제외한 로컬 산출물에만 보관한다. fixture와 실모델 실패 기록은 구분하며 실패한 모델 사전 검사는 성공한 실모델 호출로 집계하지 않는다.

<a id="completion-audit-and-limits"></a>
## 최종 확인과 한계

위에 고정한 runtime revision과 orchestrator 커밋 `02ee96343838fbb81c1dbd88c06d35b0a1129abb`로 최종 실모델 검사를 통과했다. 수용 검사 스크립트가 반환된 JSON, 어댑터 완료 이벤트 1건, 실행 span 1개, 요청 span 2개, 캐시 재조회 시 새 작업 통계 0, Langfuse 관측 ID와 부모 관계의 정확한 일치를 확인했다. 실패 상태를 보존했고 실패 영수증을 재실행하거나 삭제하지 않았다. 상위 서비스의 간헐적 라우팅 문제는 관측된 한계로 남는다.

범위에 포함된 요구사항을 계약·소유권 문서, 공통 validator와 회귀 테스트, 하위 프로세스 fixture 검사, exporter 장애 근거, 실모델 trace·캐시 검사로 대조했다. 비용 집계, 동적 작업 생성과 SMS는 범위 밖이다. 인증 정보와 세션 payload는 커밋하지 않았다.

당시 비공개 저장소 간 hosted 통합은 선택 사항이며 건너뛴 CI 작업은 통과로 집계하지 않는다. 이 단계에서 운영 배포, 범용 scheduler, 동적 작업 생성, SMS 통합, 모델 비용 집계, telemetry 내구성 보장은 추가하지 않았다.
