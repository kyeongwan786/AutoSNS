# AutoSNS

Windows desktop app for Naver Blog automation, with a local dashboard and Supabase-backed accounts. New accounts receive usage access automatically after signup verification.

## Build

See [BUILD_WINDOWS.md](BUILD_WINDOWS.md). Configure `cloud_settings.json` with the Supabase Project URL, publishable/anon key, public GitHub repository, and app version, then run `build_windows.bat` on Windows.

## Cloud services

- Run `supabase/schema.sql` in the Supabase SQL Editor.
- Deploy `supabase/functions/generate-comment` and `supabase/functions/generate-post-topics`, then store the OpenAI key in Supabase Edge Function Secrets.
- Keep the Supabase Project URL and publishable key in the public `cloud_settings.json` file; never add a service role key there.
- Push a `vMAJOR.MINOR.PATCH` tag to build and publish a Windows release.

Never put a Supabase service role key or OpenAI API key in the desktop app or `cloud_settings.json`.
