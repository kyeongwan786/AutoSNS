# AutoSNS Windows 빌드 및 백엔드 설정

## Supabase 최초 설정

1. Supabase 프로젝트를 만들고 `supabase/schema.sql`을 SQL Editor에서 실행합니다.
2. Supabase의 Project URL과 publishable/anon key를 `cloud_settings.json`에 입력합니다. Service role key는 EXE나 이 파일에 넣지 않습니다.
3. Supabase CLI로 `supabase functions deploy generate-comment`를 실행합니다. Supabase 대시보드의 Edge Function Secrets에 OpenAI 키를 `OPENAI_API_KEY` 이름으로 등록합니다. `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`는 함수의 기본 환경 변수로 사용합니다.
4. Supabase Authentication에서 Email provider의 **Confirm Email**을 켜고 비밀번호 최소 길이를 8자로 설정합니다. Authentication > URL Configuration의 Redirect URLs에 `http://127.0.0.1:8765/email-verified`를 추가해야 인증 링크가 앱으로 돌아옵니다.
5. 인증 메일을 꾸미려면 먼저 Authentication > Emails > SMTP Settings에서 사용자 지정 SMTP를 설정합니다. 새 무료 프로젝트는 기본 메일 발송을 사용할 때 템플릿 수정이 잠겨 있습니다. SMTP 설정 후 Emails > Templates > Confirm sign up에서 제목을 `AutoSNS 이메일 인증을 완료해 주세요`로 바꾸고 `supabase/confirmation-email.html` 내용을 붙여넣습니다.
6. GitHub에 **공개** 저장소를 만들고 `cloud_settings.json`의 `github_repository`를 `소유자/저장소` 형식으로 설정합니다. 업데이트 확인은 공개 GitHub Releases를 사용합니다.
7. GitHub Actions는 저장소의 공개 `cloud_settings.json`을 사용합니다. Project URL과 publishable key는 EXE에 포함되는 공개 설정입니다. Supabase Function Secrets에 보관된 OpenAI 키는 EXE에 포함하지 않습니다.

Supabase의 기본 이메일 발송은 테스트용 제한이 있으므로 실제 고객에게 배포할 때는 사용자 지정 SMTP 서비스를 연결하는 편이 좋습니다.

`supabase/schema.sql`은 새 계정에 기본 사용 권한을 자동으로 부여하고, 기존 `pending` 계정도 활성화합니다. 이미 이전 스키마를 적용했다면 SQL Editor에서 업데이트된 파일 전체를 다시 실행해야 기존 계정과 신규 가입에 자동 활성화가 적용됩니다. 이메일 인증을 켠 경우에는 인증 링크를 누른 뒤 로그인하면 사용할 수 있습니다. 댓글은 라이선스의 `daily_comment_limit` 범위 안에서 서버가 생성합니다.

## Windows EXE 만들기

Windows 10/11에서 Python 3.12를 설치하고 프로젝트 ZIP을 풉니다. `cloud_settings.json`에 위 공개 설정과 앱 버전을 입력한 뒤 `build_windows.bat`을 실행합니다. 빌드는 설정에 자리표시자가 남아 있으면 중단됩니다.

빌드 결과는 `dist\AutoSNS.exe`입니다. 이 파일 하나를 전달하면 됩니다. 받는 사람은 앱을 실행해 이메일 회원가입과 인증을 마친 뒤 로그인하면 바로 사용할 수 있습니다. 네이버 계정 정보와 자동화 기록은 각 PC의 `%APPDATA%\AutoSNS`에 별도로 저장됩니다.

## 버전 업데이트 배포

GitHub Actions의 `release-windows.yml`은 `vMAJOR.MINOR.PATCH` 형식 태그가 올라오면 Windows EXE를 빌드하고 GitHub Release에 첨부합니다. 새 빌드마다 `cloud_settings.json`의 `app_version`을 올리고 태그를 push합니다. 로그인한 앱은 GitHub Release를 확인해 새 버전이 있으면 다운로드 링크를 표시합니다.

Supabase 무료 프로젝트는 일정 기간 비활성일 때 일시 중지될 수 있습니다. 고객에게 지속 제공하기 전에 무료 플랜의 한도와 가동 조건을 확인하세요.
