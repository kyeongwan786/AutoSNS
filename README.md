# AutoSNS

Windows desktop app for Naver Blog automation, with a local dashboard and Supabase-backed accounts and licenses.

## Build

See [BUILD_WINDOWS.md](BUILD_WINDOWS.md). Configure `cloud_settings.json` with the Supabase Project URL, publishable/anon key, public GitHub repository, and app version, then run `build_windows.bat` on Windows.

## Cloud services

- Run `supabase/schema.sql` in the Supabase SQL Editor.
- Deploy `supabase/functions/generate-comment` and store the OpenAI key in Supabase Edge Function Secrets.
- Add `AUTOSNS_SUPABASE_URL` and `AUTOSNS_SUPABASE_ANON_KEY` as GitHub Actions repository variables.
- Push a `vMAJOR.MINOR.PATCH` tag to build and publish a Windows release.

Never put a Supabase service role key or OpenAI API key in the desktop app or `cloud_settings.json`.
