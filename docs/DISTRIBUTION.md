# Makara의 구성과 새 환경 설치

2026-11-28 기준. 이 문서는 공개 GitHub에서 받을 수 있는 버전과 개발 PC에서 동작하는 구성을 구분한다. 설치 버전을 결정할 때는 해당 Git 태그의 파일과 설치 문서를 기준으로 한다. 개인 위키는 프로젝트 결정과 경험을 기록하는 정본이다.

## 오늘 다른 PC에서 시작하기

Linux 또는 Windows + WSL에서 Node.js 18+, Git, Python 3.11+를 준비한다. Codex 플러그인 설치에는 호환되는 Codex CLI도 필요하다. 범용 MCP 클라이언트와 Codex 중 사용하는 쪽을 고른다.

| 목적 | 설치 명령 |
|---|---|
| Codex에서 위키와 선택 근거 조회 | `npx --yes github:Makara-space/makara#v0.4.2` |
| 다른 MCP 클라이언트 연결 | `npx --yes github:Makara-space/makara#v0.4.2 --client mcp --provider anthropic` |

두 번째 예시의 제공자는 실제 사용하는 모델 제공자에 맞춘다. 설치기가 출력한 stdio 명령과 인자를 클라이언트에 등록한다. 위키와 결정 기록부터 사용한 뒤, 자동 수집이 필요하면 허용할 프로젝트 폴더를 지정한다. Honcho를 선택하면 Honcho v3 서버와 워커를 별도로 준비한다. [설치 옵션과 호스트별 연결](INSTALL.md)

기존 Makara 저장소에 포함됐던 초기 Planetarium은 독립 설치할 수 있다. Docker와 Compose가 실행 중인 Linux/WSL에서 아래 명령으로 이번에 확인한 버전을 재현한다. 설치기는 이 사용자 계정에서 발견한 지원 에이전트의 로그를 읽어 로컬 색인을 만든다.

```sh
git clone https://github.com/Makara-space/makara.git
cd makara
git checkout --detach e9bcba6cb092d6c04a4e96a84a27d5f12baf26e8
python3 planetarium/install.py
```

공개 태그 v0.4.1로 만든 새 Linux 컨테이너에서 외부 네트워크와 기존 사용자 홈 없이 MCP 설치, 초기화, 도구 13개 조회와 인증된 상태 조회를 검증했다. 이 태그에는 Sekai와 Planetarium이 없다. 위 커밋에는 초기 Planetarium이 있다. 이후 앱 소스는 [Sekai 저장소](https://github.com/Makara-space/makara-sekai)에서 받는다. 최신 3D 관제와 Sekai 데스크톱 전체의 신규 PC 설치는 공개 릴리스로 검증되지 않았다.

## 구성별 정본과 의존성

| 구성 | 관리하는 정보 또는 역할 | 실제 의존성 |
|---|---|---|
| Makara Core | Markdown 위키, 허용된 원문과 출처, 프로젝트 범위 및 제공자 권한 | 로컬 지식 서비스. 위키만 쓰면 추론 모델 없이 시작 가능 |
| Codex 플러그인 / MCP 연결 | 에이전트에서 조회하는 방법, Codex의 허용된 대화 수집 | Core에 연결. 범용 MCP 등록과 호스트별 자동 훅 등록은 각각 설정 |
| Honcho | 허용한 대화에서 추론한 선호와 선택 이유 | 선택 서비스. 저장소·워커·추론·임베딩이 필요. Hombre 대시보드는 추가 옵션 |
| Sekai | 로컬 일정 배치, 비서 대화, 제안과 승인 | 현재 패키지는 Core의 토큰·수집 경로를 사용. 연결한 티켓의 원격 상태는 Plane이 관리 |
| 비서 실행기와 모델 | 직접 대화를 처리하고 사용할 도구와 모델을 연결 | 현재 기본 Compose는 Hermes와 소켓 중계를 전제. Gemma·OpenCode Go는 모델 제공 경로. 모델 변경과 실행기 교체는 별개 |
| Planetarium | 지원 에이전트 로그에서 재생성하는 활동·토큰 색인과 관제 화면 | Core·Honcho·Sekai 없이 설치 가능. Orca 연동 시 프로젝트·워크트리 및 열린 단말 정보를 추가 |
| Windows 데스크톱 | 캐릭터, 바탕화면 탭, 위 서비스의 화면으로 이동 | Windows/WebView2 전용. 브라우저/PWA와 별도 설치 |

## 첫 프로젝트에서 방법론 적용하기

예를 들어 발표자료를 만드는 팀이라면, 새 위키에 발표의 대상·목표·성공 기준을 먼저 적는다. 시안을 고를 때는 후보와 선택 이유를 남긴다. “큰 글씨가 좋다”보다 “심사위원이 뒤에서도 핵심 수치를 읽어야 해서 이 시안을 골랐다”가 다음 판단에 유용하다.

다음 작업을 시작할 때 에이전트는 관련 결정을 조회하고 이번 발표도 같은 조건인지 확인한다. 실제 시안을 보고 판단이 맞았는지 기록하고, 작업 단위가 끝났을 때 요청해 확정된 내용만 위키에 반영한다. 기억 추론을 켜면 Honcho는 반복되는 선호의 후보를 찾고, 일정 관리가 필요하면 Sekai는 승인한 작업을 티켓과 연결한다. Planetarium은 어떤 조사·수정에 시간이 들었는지 검토할 때 추가한다. 이 순서로 첫 선택이 다음 작업에 쓰이는지 작은 예시 하나로 확인할 수 있다.

## 공개 저장소의 역할

**Makara-space 조직에는 두 제품 저장소가 있다.** [Makara](https://github.com/Makara-space/makara)는 에이전트에서 사용하는 지식 서비스와 플러그인이다. [Sekai](https://github.com/Makara-space/makara-sekai)는 바탕화면과 PWA에서 사용하는 앱이며 Planetarium 소스도 이곳에 있다.

| 원격 | 소유하는 소스와 문서 | 릴리스에서 책임지는 경험 |
|---|---|---|
| `Makara-space/makara` | `makara/`, `plugins/makara/`, 기존 npx·MCP 설치기, 수집 훅, Obsidian 연결, 선택적 Honcho 연동, Core 배포 파일과 그 테스트 | 에이전트에서 위키와 허용된 기억을 조회한다. Sekai를 설치하지 않아도 기존 플러그인 명령이 동작한다. |
| `Makara-space/makara-sekai` | `sekai/`, `planetarium/`, Windows 위젯·PWA·화면 자산, 비서 모델 및 Hermes 어댑터, 두 앱의 설치 설정·테스트·고지 | Sekai에서 일정을 조율하고 같은 바탕화면에서 Planetarium 관제로 이동한다. 로그 관제만 원하는 사용자는 Planetarium만 선택해 실행한다. |

바탕화면 셸, 캐릭터, 공통 탐색, 화면 간 전환 문제는 **Sekai**에서 수정한다. “어떤 원문을 어떤 에이전트에게 보여줄 것인가”와 MCP 호환성은 **Makara**에서 수정한다. 예를 들어 공개 범위가 바뀌면 Core가 검사하고 Sekai는 거부 상태를 화면에 표시한다. 모델 제공자는 비서 실행기와 별도 설정으로 둔다. Plane은 티켓 API, Honcho는 기억 API로 연결한다.

### 두 원격을 함께 설치하는 방법

첫 배포에서는 **Sekai 릴리스가 전체 설치의 진입점**을 맡는 방향을 권한다. 릴리스마다 호환되는 Makara 태그 또는 커밋, 사용 이미지의 버전, 지원 OS를 명시한다. 설치기는 기존 Core의 상태·인증·호환성을 먼저 확인하고, 비어 있는 환경에서 필요한 구성만 준비한다. 새 PC의 키와 프로젝트 경로를 받아 로컬 연결 설정을 발급한다. 이미 운영하는 Core를 새 설치로 덮어쓰면 안 된다.

- **지식 플러그인만** 선택하면 기존 Makara 설치기를 사용한다.
- **활동 관제만** 선택하면 앱 저장소에서 Planetarium을 실행한다. 위키, 모델 계정, Plane이 필요하지 않다.
- **비서와 바탕화면**은 Sekai와 필요한 Core 연결을 설치한다. Planetarium 탭, Windows 위젯, Plane 연결, 비서 실행기, 개인화 기억은 원하는 기능에 맞춰 고른다. 직접 모델 호출과 Hermes 실행은 각각 새 환경에서 검증한 뒤 설치 메뉴에 표시한다.
- 두 저장소의 연결 기준은 버전이 있는 인터페이스다. 현재 Sekai Compose가 직접 마운트하는 `sources`와 토큰 경로, Hermes의 별도 relay 소켓까지 의존성에 포함한다. Core 원문 감지는 장기적으로 권한을 검사하는 변경 피드로 연결하는 편이 경로 이전과 접근 제어에 유리하다.

GitHub에서 보이는 소속을 묶고 싶으면 **Organization**, 여러 저장소의 이슈·PR을 한 보드에서 보려면 **Projects**를 쓸 수 있다. 조직 프로필에서 두 제품으로 연결하며, 버전과 설치 절차는 각 제품 저장소에서 관리한다. **Git submodule**은 다른 저장소의 특정 커밋을 소스 트리에 고정한다. 설치할 서비스·데이터·권한을 선택하는 역할은 Sekai 설치기가 맡도록 한다. 세 번째 `makara-space` 원격은 공통 배포물을 별도 팀이나 독립 일정으로 관리할 필요가 생길 때 검토한다. [Organizations](https://docs.github.com/en/organizations/collaborating-with-groups-in-organizations/about-organizations) · [Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/about-projects) · [Git submodules](https://git-scm.com/book/en/v2/Git-Tools-Submodules)

### 릴리스 유지관리 절차

1. 파일과 테스트를 위 두 소유자로 분류한다. 예를 들어 `deploy/Dockerfile`은 Core에, `deploy/hermes/`는 Sekai에 둔다. 두 저장소 모두 공개할 파일과 Git 이력, 에셋 출처를 검토하고 개인 운영 기록을 예제 설정으로 바꾼다.
2. 현재 Core에 필요한 변경을 기존 공개 저장소의 깨끗한 기준점에서 준비한다. 검증한 Core 태그를 앱 저장소가 참조한다.
3. 검토한 Sekai와 Planetarium을 앱 저장소에 함께 담고, 설치 프로필과 버전 조합을 빈 환경에서 시험한다. 최소 설치는 MCP 조회, Sekai는 임시 대화·일정 왕복, Planetarium은 예시 로그 표시, 선택 연동은 누락·실패·성공 상태를 확인한다.
4. 앱의 첫 공개 태그가 동작하면 기존 Makara README와 초기 Planetarium 설치 경로에 이전 안내를 둔다. 기존 태그와 사용 중인 설치 명령을 유지한다. 사용자 위키, 로컬 일정, 에이전트 로그와 키는 별도 비공개 이전 대상으로 취급한다.

## 공개할 소스와 외부 구성요소

저장소의 자체 소스에는 Apache-2.0을 적용한다. 실제로 배포하는 소스·브라우저 번들·컨테이너·다운로드 모델별로 포함된 제3자 구성요소의 라이선스 및 고지를 확인한다.

- Planetarium의 vendored Three.js에는 MIT 고지 파일이 있다. 프런트엔드 번들과 함께 유지한다. [Three.js 라이선스](https://github.com/mrdoob/three.js/blob/dev/LICENSE)
- Honcho, Plane Community Edition과 Hombre는 각각 AGPL 계열 상위 프로젝트다. 외부 서비스 연결, 수정한 상위 파일 제공, 이미지 재배포를 구분해 실제 배포물 기준으로 조건을 확인한다. [Honcho](https://github.com/plastic-labs/honcho/blob/main/LICENSE) · [Plane](https://github.com/makeplane/plane/blob/preview/LICENSE.txt) · [Hombre](https://github.com/lovethatbrandx/hombre)
- Hermes, whisper.cpp, Whisper 가중치, 이미지 안의 Poppler와 Windows WebView2 SDK 등도 배포 위치와 버전을 기록한다. 캐릭터·배경·아이콘은 원본, 제작 경위와 배포 권리를 확인한다. 음성 모델은 현재 Sekai 이미지 빌드 중 다운로드된다. [Hermes](https://github.com/NousResearch/hermes-agent) · [whisper.cpp](https://github.com/ggml-org/whisper.cpp) · [Whisper](https://github.com/openai/whisper) · [Poppler](https://poppler.freedesktop.org/)
- 공개 후보는 파일 목록과 반영할 Git 이력 양쪽을 점검한다. 운영 문서의 사용자 경로·내부 접속 주소는 예제로 바꾸고, 실제 vault, DB, 에이전트 로그, 키, OAuth 파일, 개인 첨부 및 복구본은 공개 배포물에서 제외한다.
