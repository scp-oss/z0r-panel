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
