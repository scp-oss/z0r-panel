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
