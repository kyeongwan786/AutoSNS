# AutoSNS Windows 빌드 및 백엔드 설정

## Supabase 최초 설정

1. Supabase 프로젝트를 만들고 `supabase/schema.sql`을 SQL Editor에서 실행합니다.
2. Supabase의 Project URL과 publishable/anon key를 `cloud_settings.json`에 입력합니다. Service role key는 EXE나 이 파일에 넣지 않습니다.
3. Supabase CLI로 `supabase functions deploy generate-comment`를 실행합니다. Supabase 대시보드의 Edge Function Secrets에 OpenAI 키를 `OPENAI_API_KEY` 이름으로 등록합니다. `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`는 함수의 기본 환경 변수로 사용합니다.
4. GitHub에 **공개** 저장소를 만들고 `cloud_settings.json`의 `github_repository`를 `소유자/저장소` 형식으로 설정합니다. 업데이트 확인은 공개 GitHub Releases를 사용합니다.
5. GitHub Actions는 저장소의 공개 `cloud_settings.json`을 사용합니다. Project URL과 publishable key는 EXE에 포함되는 공개 설정입니다. Supabase Function Secrets에 보관된 OpenAI 키는 EXE에 포함하지 않습니다.

회원가입한 사용자는 Supabase Auth에서 확인한 뒤 `customer_licenses` 테이블에 라이선스 행을 추가합니다. 예시:

```sql
insert into public.customer_licenses (user_id, status, expires_at, notes)
values ('SUPABASE_AUTH_USER_UUID', 'active', now() + interval '30 days', '고객 메모');
```

접근을 끊으려면 해당 사용자의 `status`를 `suspended`로 바꾸면 됩니다. 댓글은 라이선스의 `daily_comment_limit` 범위 안에서만 서버가 생성합니다.

## Windows EXE 만들기

Windows 10/11에서 Python 3.12를 설치하고 프로젝트 ZIP을 풉니다. `cloud_settings.json`에 위 공개 설정과 앱 버전을 입력한 뒤 `build_windows.bat`을 실행합니다. 빌드는 설정에 자리표시자가 남아 있으면 중단됩니다.

빌드 결과는 `dist\AutoSNS.exe`입니다. 이 파일 하나를 전달하면 됩니다. 받는 사람은 앱을 실행해 이메일 회원가입 후 관리자가 라이선스를 활성화할 때까지 기다린 다음 로그인합니다. 네이버 계정 정보와 자동화 기록은 각 PC의 `%APPDATA%\AutoSNS`에 별도로 저장됩니다.

## 버전 업데이트 배포

GitHub Actions의 `release-windows.yml`은 `vMAJOR.MINOR.PATCH` 형식 태그가 올라오면 Windows EXE를 빌드하고 GitHub Release에 첨부합니다. 새 빌드마다 `cloud_settings.json`의 `app_version`을 올리고 태그를 push합니다. 로그인한 앱은 GitHub Release를 확인해 새 버전이 있으면 다운로드 링크를 표시합니다.

Supabase 무료 프로젝트는 일정 기간 비활성일 때 일시 중지될 수 있습니다. 고객에게 지속 제공하기 전에 무료 플랜의 한도와 가동 조건을 확인하세요.
