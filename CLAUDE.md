# CLAUDE.md

Operational notes for Claude sessions working on this repo — dense, for an
agent, not prose for an external reader (that's what README.md is for).
Do not put server names, ISP/provider names, individual people's names,
or unpublished/draft project names here — same public-repo constraint as
README.md. `Server A`/`Server B`/etc. and `Provider A`/`Provider B`/etc.
are anonymized codenames — see z2r_autobench's own CLAUDE.md for the
convention.

## The panel is a module, not the backend — this is a hard constraint, not a preference

- Standing principle (see README.md "Границы ответственности" for the
  full history): **the panel is a thin remote control ("пульт"), not
  where logic lives.** Real work — geноme generation, mutation, UCB,
  promotion, sandbox testing — belongs in Zenith's own `orchestrator/`
  and in `z2r_autobench`'s own CLI scripts. The panel calls into them
  (`set_strategy_cli.sh get/max/set`, `systemctl start/stop/status`,
  spawning `main.py`/`auto_promoter.py` as subprocesses) — it does not
  reimplement or duplicate their logic in Python here.
- Concretely: `zenith-autorun.service` (periodic candidate generation)
  and `zenith-promoter.service` (autonomous promotion) are systemd units
  that live in the **Zenith** repo, not this one. This repo's
  `daemon_ctl.py`/`autoupdate_ctl.py`/the `/controls` routes only
  start/stop/tail-log them — if this panel is uninstalled, disabled, or
  the process crashes, both units keep running completely unaffected.
  Never move their actual logic into this repo "for convenience" — that
  would silently make the autonomous pipeline depend on the panel being
  up, breaking the whole point of the split.
- Before adding a new feature here, ask: could this run correctly with
  no panel installed at all, purely from `z0r`/Zenith's own CLI? If the
  honest answer is no because the logic only exists in this repo, that
  logic is in the wrong repo — move it to Zenith/z2r_autobench and have
  the panel call it, not the other way around.
- This also means: don't add state here that only the panel knows about
  and that Zenith/z2r_autobench would need to function correctly. The
  shared MySQL (`z2r_genome`) is the one exception — it's shared
  infrastructure, not panel-owned state (see "Как это устроено" in
  README.md — the panel is just an ordinary client of the same DB the
  local orchestrator already writes to, not a separate store).

## `/controls` split into two pages (since 2026-08-29) — same actions, different presentation

- UX pass: the single `/controls` page had grown to 7+ stacked cards
  (profile status, manual strategy switch, domain test runner, zenith-
  autorun, zenith-promoter, autotune-daemon, autoupdate, per-profile
  genomes) — no way to jump between them but scrolling. Split into
  `/controls` ("Стратегии": run/test-domain card, profile status table,
  manual strategy switch, per-profile genomes) and a new `/controls/automation`
  ("Автоматизация": zenith-autorun/zenith-promoter/autotune-daemon/
  autoupdate — the "set it and forget it" background units).
- **Zero backend logic changed to do this.** `_controls_context()` still
  builds the exact same full context dict on every call, for both routes
  — the split is purely about which template (`controls.html` vs the new
  `automation.html`) renders it. Every POST action handler (`/controls/
  daemon/start`, `/controls/zenith-autorun/*`, `/controls/zenith-promoter/*`,
  `/controls/autoupdate/*`) now re-renders `automation.html` instead of
  `controls.html` after doing its thing — same subprocess/systemctl/sudo
  calls as before, just a different template picked afterward. The GET
  JSON status endpoints (`/controls/run/status`, `/controls/daemon/status`,
  etc.) are unaffected either way — they don't render a template, and
  both pages' polling JS just fetches the same URLs regardless of which
  page it's running on.
- Sidebar nav (see `base.html`) reflects this: "Стратегии" is active only
  on exact path `/controls` (not a prefix match), "Автоматизация" only on
  `/controls/automation` — needed exact vs. prefix distinction so the two
  don't both light up when the other is showing.
- If a new automation-style toggle gets added later (another systemd
  unit, another periodic job), its POST handlers should render
  `automation.html`, not `controls.html` — that's now the "background
  services" page, `controls.html` is "things you actively do".

## Funnel-testing a custom domain from `/controls` (since 2026-08-29)

- Distinct third launcher, alongside `runner.py` (Zenith `main.py`,
  sandbox-only genome search) and the systemd unit wrappers
  (`daemon_ctl.py`/`autorun_ctl.py`/etc.): `funnel_runner.py` shells out
  to `z2r_autobench/rank_strategies.sh --domain X --funnel --passes N`.
  **This one is NOT sandbox-isolated** — `rank_strategies.sh` directly
  flips the real production strategy for the profile that governs the
  given domain and probes it with live traffic, then reverts at the end
  (see its own docstring's "побочный эффект" warning, surfaced verbatim
  in the new `#sec-funnel` card on `controls.html`). This is a genuinely
  different risk class from `runner.py`'s button right above it on the
  same page — same page, same "Стратегии" grouping, very different blast
  radius; the card's copy spells this out so a user doesn't assume both
  buttons are equally safe to mash.
- Why a separate module instead of extending `runner.py`: `runner.py`'s
  own docstring scopes it explicitly to "запускает `main.py` тем же
  способом... " — different target script, different output format
  entirely (`rank_strategies.sh` prints `--- Проход N/M (кандидатов: X)
  ---` / `проход N завершён, выжило кандидатов: Y` / `ORDERED_SUCCESS: ...`,
  nothing like `main.py`'s `[N/M] ... op=... -> ...` round format that
  `runner.py::_ROUND_RE`/`_RESULT_RE` parse), and critically:
  `rank_strategies.sh` already does its own locking
  (`acquire_tune_lock`/`TUNE_LOCK_FILE`, shared with `autotune_daemon.sh`
  and friends in z2r_autobench) so the launcher doesn't need `runner.py`'s
  `sudo -n flock -n RUN_LOCK_FILE` wrapper trick at all — a second launch
  attempt just gets rejected by the script itself with a clear message,
  surfaced through `error_tail` like any other failure.
- Domain→profile routing is **not duplicated here** — the panel only ever
  passes the raw domain string through; `rank_strategies.sh --domain`
  resolves which real numeric profile governs it and reports back via a
  `Домен X -> профиль N (TITLE)` line that `funnel_runner._parse_log()`
  picks up as `profile_info` for display. If this ever needs to be shown
  BEFORE launching (e.g. a live preview as the user types a domain), that
  lookup would need its own read-only endpoint calling into
  `z2r_detect_governing_profile()` — doesn't exist yet, not needed for
  the current one-shot "type domain, click, watch it run" flow.
- `_parse_log()` tracks `pending_round` the same way `runner.py::_parse_log`
  does for genome rounds — a `--- Проход N/M ---` header line with no
  matching `проход N завершён...` line yet means the script is mid-pass
  (each candidate in the pass = strategy switch + settle sleep + live
  probe, can take a while) — surfaced in `controls.html` as "вычисление
  результатов…" with the same striped/animated progress-bar treatment as
  the genome-round pending state above it on the page.
- Real-time successes: `rank_strategies.sh`'s funnel loop now also prints
  a `strategy=N -> OK (bytes, проход P/PASSES)` stdout line the instant a
  candidate survives a pass (see z2r_autobench's own CLAUDE.md for why —
  added purely for this parser) — `funnel_runner.py` surfaces these as
  they land, satisfying the live request "if one succeeds while the test
  is still running, show it, and keep showing more as they appear" rather
  than only revealing results once a whole pass (or the whole run)
  finishes.
- No `--apply` flag is ever passed from the panel — the funnel only
  measures and reverts, same principle as the panel's other
  strategy-mutating actions ("Ручное переключение стратегии" section
  right below it on the same page): the panel picks nothing, applies
  nothing on its own initiative. Seeing a winning strategy in the funnel
  results is the cue for a human to go set it manually via that section,
  not something this feature does automatically.

## `/custom-domains` — exotic domains with their own independent strategy (since 2026-08-29)

- Distinct from `/rkn`: that page manages domains that share ONE strategy
  (the whole RKN_TLS profile's). This page is for domains where that
  doesn't work — each gets its OWN brand-new numeric profile in
  `/opt/zapret2/config`, entirely independent of every other domain's.
  See `z2r_autobench/custom_domain_cli.sh`'s own docstring/CLAUDE.md for
  why this needs a genuinely new config block (not just a strategy
  switch) and how it's generated safely (clones the real, live RKN_TLS
  block instead of guessing at nfqws2 syntax).
- **This is the riskiest write this panel can trigger** — every other
  mutating action here (`set_strategy_cli.sh set`, `rkn_list_cli.sh add`,
  even the funnel) only ever flips a value or a line in an existing,
  known-good structure. `/custom-domains/add` can append a brand-new
  block to the live production config. The two-step UI (`preview` then a
  separate, explicitly-confirmed `add`) mirrors the CLI's own `--yes`
  gate exactly — the panel does not collapse this into one click, and the
  preview's exact stderr text (the literal block that would be written,
  or the refusal reason) is shown verbatim, not summarized, so a human
  can actually read it before confirming.
- `_run_custom_domain_cli()` is a third variant of the panel's
  `sudo -n bash <script>` wrapper pattern (`_run_cli`, `_run_rkn_cli`,
  now this) — needed because `custom_domain_cli.sh` puts its real content
  on **stderr** for `add`/`remove` (`list`'s table is on stdout) and the
  distinction between "successful preview" and "refused" is carried by
  the exit code alone, not by which stream has content — both `_run_cli`
  and `_run_rkn_cli`'s existing contracts (return `None` on any failure,
  or fold everything into one stdout/error pair) don't fit that shape.
- Removing a domain here never edits config structure (see the CLI's own
  reasoning) — it only empties that domain's dedicated hostlist file, so
  the panel's remove button is safe to expose without the same
  preview/confirm ceremony as add.

## `/domains` — per-profile test domain_pool, split out of `/rkn` (since 2026-08-31)

- `/rkn`'s old "тестовый список" section (`domain_pool` CRUD, used by
  Zenith's genome testing and now `auto_promoter.py`'s live-check) was
  hardcoded to `RKN_TLS` — by direct request this is architecturally
  wrong: "мы же с тобой договорились давно уже что для каждого профиля
  свой тестер... YouTube весьма специфичный, мы не будем его вносить в
  РКН список" (each profile gets its own independent tester; YouTube's
  domains must never be mixed into the RKN hostlist/mechanism). Moved to
  a new `/domains?profile=X` page covering any profile in
  `DOMAIN_LIST_PROFILES`.
- Zero new DB logic needed — `db.list_domains_for_profile()`/
  `get_or_create_domain()`/`delete_domain()` were already
  profile-parameterized (the first one's own docstring literally
  anticipated this: "для страницы /rkn (и любого другого будущего
  'список доменов профиля' в панели)"). This page is that "future" page.
- `/rkn` now shows ONLY the production hostlist (`TCP_RKN_list.txt`/
  `TCP_Custom.txt`) — the old `/rkn/add` and `/rkn/{id}/delete` routes are
  gone, replaced by `/domains/add` and `/domains/{id}/delete` (both take
  an explicit `profile` field/hidden input rather than assuming RKN_TLS).
- Live trigger for this split: while diagnosing the YT_TLS/WebOS incident
  (see z0r-panel's/zenith's own commit history same day —
  `auto_promoter.py`'s live-check gap), the natural next question was
  "why does the live-check only test one www.youtube.com row instead of
  several real YouTube domains" — the honest answer was that
  `domain_pool` for YT_TLS only ever had one row, and the only UI to add
  more (`/rkn`) was scoped to the wrong profile entirely.
- **"Синхронизировать" button** (`/domains/sync`) — for profiles that
  have one, reads a real official curated domain list already sitting on
  the server (`z2r_autobench/domain_list_sync.sh`, e.g.
  `/opt/zator/lists/russia-youtube.txt`) instead of asking a human to
  copy-paste. `sync_available` in the template context comes from
  actually asking the CLI (`--list-profiles`) which profiles it knows
  about, not a second hardcoded list here — the CLI's own dict is the
  one place that mapping lives, avoiding yet another instance of the
  "same fact duplicated in two files, one goes stale" class of bug this
  whole engagement keeps running into (see z2r_autobench's CLAUDE.md
  `/opt/zapret2` vs `/opt/zator` saga for the fullest writeup of that
  pattern). Feeds through the exact same `get_or_create_domain()` as
  manual/bulk add — no separate "came from sync" bookkeeping.
- **`DOMAIN_LIST_PROFILES` narrowed 2026-08-31** (direct request, "нет
  подпункта для yt quic, а FB_TLS/FB_HTTP наверное пока стоит убрать"):
  now `["YT_TLS", "GV_TLS", "YT_QUIC_UDP", "RKN_TLS", "DS_TLS"]` —
  `FB_TLS`/`FB_HTTP` removed (Zenith can't run genomes against either one
  at all yet — not in `genome.PROFILE_FILTER_TYPE`, not in `main.py
  --profile` choices, not in `runner.RUNNABLE_PROFILES`; same `[dev]`-stub
  status as `GAMES_UDP`, so their domain lists were dead weight with no
  way to actually drive a run against them). Existing `domain_pool` rows
  for those two profiles were NOT deleted, just no longer reachable via
  the nav — re-add the strings to the list if/when Zenith ever implements
  them. `YT_QUIC_UDP` added alongside `GV_TLS`, and with the exact same
  caveat: the real QUIC edge (`googlevideo.com`) is resolved dynamically
  every round via `yt-dlp` (`gv_resolver.py` in Zenith,
  `rank_quic.sh`/`quic_probe.py` in z2r_autobench) — a `domain_pool` row
  here is never the actual host tested, it only exists as a placeholder
  for `domain_id`/`min_bytes` bookkeeping (see Zenith's `main.py::run`,
  `if profile == "GV_TLS"` branch — `YT_QUIC_UDP` isn't wired into that
  branch yet since Zenith doesn't runnable-support it either, so today
  this entry is UI-only consistency with `GV_TLS`, not yet backed by an
  actual Zenith run path).

## Restart buttons on `/controls/automation` (since 2026-08-31)

- `daemon_ctl.SystemdServiceCtl` gained a `.restart()` alongside its
  existing `.start()`/`.stop()`/`.is_active()`/`.log_tail()` — same
  narrow `sudo -n systemctl <verb> <unit>` shape, one new sudoers line
  per unit in `z0r::ensure_panel_runtime_grants` (`restart` added next to
  each unit's existing start/stop/is-active/journalctl grant, nothing
  widened beyond that). All three panel-managed units
  (`autotune_daemon`/`zenith_autorun`/`zenith_promoter`) get a "рестарт"
  button between start and stop on `automation.html`.
- This mirrors a parallel CLI-side change in `z2r_autobench`'s `z0r`
  (uniform restart/stop submenu for every `manage_X()`, see its own
  CLAUDE.md) — same live request, applied to whichever half of each
  module already has a panel presence. Discord_bot/DNSCrypt-proxy/
  Zenith-TG/web_panel have no panel page at all today, so they only got
  the CLI-side treatment — adding panel pages for those is a separate,
  bigger piece of work than this pass covered.

## "проверить обновления" for the three panel-managed daemons (since 2026-08-31)

- Mirrors a same-day CLI-side change in `z2r_autobench`'s `z0r`
  (`_check_git_updates()`, wired into its own six modules' restart/stop
  submenu) — same live request, applied to whichever half of each module
  already has a panel presence. `daemon_ctl.check_git_updates(repo_dir)`
  runs the identical three-command sequence
  (`git fetch origin main` / `rev-list --count HEAD..origin/main` /
  `log --oneline HEAD..origin/main`) — kept as a separate Python
  implementation (not a shared script) since it runs in a different
  process/language than `z0r`'s bash version, but the actual git
  commands and semantics are deliberately identical so a status message
  means the same thing regardless of which surface reports it. It is
  **read-only** — never runs the actual `pull`, that stays `z0r`
  item 25's (Автообновление) job; this only tells you whether visiting
  it is worth it.
- `config.ZENITH_DIR` (new) is the Zenith repo ROOT, not
  `ZENITH_ORCHESTRATOR_DIR` — `zenith-autorun` and `zenith-promoter`
  both live in that one repo, so their two "проверить обновления"
  buttons point at the same directory (checking either gives the
  identical answer — expected, not a bug). `autotune-daemon`'s button
  points at `config.Z2R_AUTOBENCH_DIR` instead (this repo's sibling
  directory, i.e. `z2r_autobench` itself), since that's where
  autotune-daemon actually lives, not Zenith.
- Sudoers grant lives in `z2r_autobench`'s `z0r::ensure_panel_runtime_grants`
  (not here) — three literal `git -C <dir> <verb> ...` lines per repo,
  no `*` wildcards, matching exactly what `check_git_updates()` invokes.
