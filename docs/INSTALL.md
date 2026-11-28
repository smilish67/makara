# 설치와 Honcho 연결

Codex의 로컬 플러그인으로 프로젝트 위키를 조회하고, 이전 선택의 근거를 꺼내 첫 시안을 검토합니다. Markdown 위키는 사용자 요청 시 갱신합니다. 허용한 폴더의 대화 수집과 Honcho의 비동기 기억 생성은 각각 선택할 수 있습니다. [컨셉과 첫 시안 운영 방식](CONCEPT_WORKFLOW.md)

## 설치

Claude Code에서는 실제 연결 상태를 확인했습니다. OpenCode 2.3.0에서는 설정 파일 등록을 확인했으며 연결 목록 표시는 아직 검증 중입니다.

로컬 서비스는 Linux 또는 Windows WSL에서 Python 3.11 이상으로 실행합니다. Codex 플러그인을 선택할 때는 `codex plugin add`를 지원하는 Codex CLI도 필요합니다. Node 없이 설치하려면 소스를 받아 아래 명령을 실행합니다. GitHub의 **Code → Download ZIP**으로 받아 압축을 풀어도 됩니다.

```sh
git clone https://github.com/Makara-space/makara.git
cd makara
python3 scripts/install_plugin.py
```

Windows PC 앱이 WSL 에이전트 환경을 쓰는 경우에는 같은 WSL 배포판에서 마지막 명령을 `python3 scripts/install_plugin.py --windows-app`으로 실행합니다. 설치 후 앱을 다시 열고 새 대화에서 `$makara:makara`를 선택합니다. 호스트가 훅 신뢰 확인을 표시하면 해당 훅을 검토합니다.

기존 Makara 서비스가 있으면 설치기는 그 서비스와 vault를 재사용합니다. 처음 설치하면 사용자 홈의 `MakaraVault`에 Markdown을, `~/.local/share/makara`에 서비스 설정과 인증 파일을 두고 로컬 서버를 시작합니다. 첫 설치에서 vault를 선택하려면 `--vault "/원하는/vault"`를 추가합니다. 새 설치의 기본값에서는 대화를 자동 수집할 폴더가 비어 있습니다. 재부팅 후 직접 실행형 서비스가 꺼졌으면 설치 명령을 다시 실행할 수 있습니다.

## 다른 MCP 클라이언트에서 사용하기

`python3 scripts/install_plugin.py --client mcp --provider anthropic`는 Codex 없이 서비스를 시작하고 `mcpServers.makara`의 실행 명령과 인자, 설정 파일 경로를 출력합니다. 프로젝트를 지정하려면 `--project-dir`를 추가하세요. 이 모드에서는 프로젝트 접근 범위만 설정합니다. 대화 수집 훅은 등록하지 않습니다. 도구로 명시적으로 자료를 저장하거나 사용자 요청에 따라 위키를 갱신할 수 있습니다.

- **Claude Code** — Linux/WSL에서 아래처럼 생성된 항목을 등록합니다. 현재 프로젝트에만 적용되며 설정 파일에는 인증 파일의 경로가 들어갑니다.
- **OpenCode** — 실제 모델 제공자를 지정해 설치합니다(예: `--provider deepseek`). 출력된 `command`와 `args`를 순서대로 `opencode mcp add makara -- <command> <args...>`에 전달하고 `opencode mcp list`로 확인합니다. [OpenCode MCP 안내](https://opencode.ai/docs/mcp-servers/)
- **Windows 네이티브 클라이언트** — PowerShell의 npx 명령은 WSL 실행 명령을 출력합니다. 클라이언트가 WSL 안에서 실행된다면 WSL 터미널에서 설치해 Linux 실행 명령을 받으세요.

```sh
python3 -c 'import json,pathlib,sys; p=pathlib.Path.home()/".local/share/makara/mcp-anthropic.json"; json.dump(json.loads(p.read_text())["mcpServers"]["makara"],sys.stdout)' | claude mcp add-json makara -
```

`--provider`는 조회 권한을 구분하는 데 사용합니다. 실행 호스트가 실제 사용하는 제공자와 일치시켜야 하며, 제공자를 바꾸면 그에 맞는 연결을 선택하세요. 기존 출처의 공개 범위는 자동으로 확대되지 않습니다. 스킬 파일 검색 방식과 자동 대화 수집은 각 호스트에 따라 다릅니다.

## 대화 수집과 Honcho 선택

| 원하는 동작 | 설치기에 추가할 옵션 |
|---|---|
| 위키와 스킬만 사용 | 기본 설치 |
| 지정한 폴더와 하위 폴더의 사용자 입력과 최종 응답을 수집 | `--project-dir "/프로젝트/폴더"` |
| 위 폴더에 대해 기존 Honcho v3에 기억 생성 요청 | 프로젝트 폴더와 함께 `--honcho-url`, 인증을 쓰면 `--honcho-token-file` |
| 명시적으로 원격 Honcho 사용 | 위 옵션에 `--allow-remote` 추가. 원격 주소는 HTTPS를 사용 |

예를 들어 WSL에서 실행 중인 로컬 Honcho를 Windows PC 앱에 연결하는 명령입니다. 프로젝트 경로와 토큰 파일 경로는 실제 값으로 바꿉니다.

```sh
python3 scripts/install_plugin.py --windows-app \
  --project-dir "/path/to/my-project" \
  --honcho-url http://127.0.0.1:18766 \
  --honcho-token-file "$HOME/.local/share/makara/honcho/admin.token"
```

Honcho는 **선택적인 서버 의존성**입니다. API와 기억 추론 워커가 준비된 Honcho v3를 사용합니다. 모델 서버(예: LM Studio의 1234 포트), 대시보드, Markdown 저장 경로는 각각 다른 역할입니다. 토큰은 서비스가 읽을 수 있는 비공개 파일에 둡니다. 원격 서비스의 인증·권한은 Makara 전용 workspace 생성, 조회, 메시지 추가, 정정·삭제에 필요한 작업을 지원해야 합니다.

`--project-dir`는 수집에 동의하는 로컬 폴더입니다. Honcho의 저장 영역은 Makara가 프로젝트 범위, 사용자, 제공자를 기준으로 전용 workspace를 만들어 관리합니다. 임의의 기존 workspace 이름을 채팅에 알려줄 필요가 없습니다. 기존 설치에 옵션 없이 실행하면 이미 쓰던 Honcho 주소와 수집 정책을 사용합니다. Docker로 따로 운영하는 Makara 서비스에서 설정을 바꾼 경우 서비스 프로세스를 재시작해야 새 설정이 적용됩니다. 컨테이너 안에서도 Honcho 주소와 토큰 파일 경로에 접근할 수 있어야 합니다.

## 확인

새 대화에서 “마카라 수집 상태와 현재 프로젝트 범위를 확인해줘”라고 요청합니다. 저장소에서 `python3 scripts/check_plugin.py <설치된-플러그인-폴더>`를 실행하면 MCP 인증 연결을 확인할 수 있습니다. 훅은 대화를 수집하고, 새 컨셉에서 질문을 꺼내는 일은 스킬을 읽은 에이전트가 맡습니다. Honcho 워커가 추론을 마쳐야 새 기억을 조회할 수 있습니다.

이미 Makara 서비스가 있는 Linux 환경을 위한 플러그인 ZIP은 [릴리스](https://github.com/Makara-space/makara/releases)에서 받을 수 있습니다. 새 PC나 Windows PC 앱에서는 위 설치기를 사용하면 서비스와 호스트별 실행 경로까지 설정됩니다.
