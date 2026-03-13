# Email Monitor

## Stack
- **Supabase** — DB, auth, edge functions | **Railway** — Python worker (`worker/`)
- **Extension** — Chrome MV3 (`extension/`) | **Dashboard** — Frontend (`web/`)

## Env Vars
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (worker), `SUPABASE_ANON_KEY` (client), `ANTHROPIC_API_KEY`

## CLI Rules
- **Always link first:** `supabase link --project-ref <ref>` / `railway link`
- **Check status before acting:** `railway status` to verify environment
- Run `/project:supabase` or `/project:railway` for CLI command reference
