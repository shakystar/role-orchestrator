# 독립 리뷰팀 배정

상위 오케스트레이터는 팀 ID, 업무, 검토 유형, 상태 처리 변경 여부, 산출물 작성자 ID 목록, 검토할 증거를 전달한다. 팀 내부의 배정기는 유형과 이해충돌 여부를 확인하고 고정된 명부 버전으로 담당자를 실행한다. 기준이나 대상이 불분명한 작업은 상위 호출자가 구체화한다.

프로젝트 루트에서 다음과 같이 호출한다. `LAB_SHA`에는 리뷰팀 명부를 등록한 전체 커밋 해시를 지정한다. 한 업무 개정에는 하나의 상태 디렉터리를 사용한다.

```sh
python -m role_orchestrator.review_team \
  --lab "$LAB" --revision "$LAB_SHA" --runtime "$BRIDGE" \
  --state "$STATE" --backend codex < review-request.json
```

```json
{
  "team": "independent-review",
  "task": "승인 기준이 재시작 뒤에도 확인 가능한지 검토한다.",
  "kind": "criteria",
  "stateful": false,
  "authors": ["acl-001"],
  "evidence": {
    "criterion": "같은 수정 작업을 재승인해도 새 제출은 0건이어야 한다.",
    "measurement": "승인 전후의 런타임 작업 ID 집합과 실행 영수증을 대조한다."
  }
}
```

| 검토 유형 | 전문 검토자 |
|---|---|
| criteria | 이서진 · 인수 기준 |
| counterexample | 조하린 · 독립 반례 |
| patch | 신도윤 · 코드, 김가온 · 재현, 장유나 · 출처. 상태 처리 변경 시 서민재 · 복구 추가 |
| reproduction | 김가온 · 재현 |
| provenance | 장유나 · 출처 |

전문 검토가 수락되면 강이현이 `review-v1` 프로필로 최종 판정한다. 각 역할에는 업무와 증거, 선행 검토 산출물을 전달한다. 보완 요청에는 `decision`, `reason`, `owner`를 담는다. 작성자 목록에는 실제 작성에 참여한 구성원을 모두 기재한다. 최종 인수와 릴리스 결정은 별도로 기록한다.

## 검증 범위

명부 커밋 `72fe33e40fdf505019b41b52679e851ebfbd16df`에서 일곱 명의 fixture 영수증을 확인했다. 합성 기준의 실모델 검토에서는 이서진이 최초 입력의 계측 근거를 반려했다. 수정된 기준은 이서진과 강이현이 차례로 수락했다. 새 제출 없이 읽기 전용 상태와 네트워크 차단 환경에서 두 영수증으로 판정을 다시 조회했다. [실행 식별자와 판정](../validation/independent-review.json)
