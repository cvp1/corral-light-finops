"""Entry points: the collector and the CLI verbs."""
import json
import re
import sys
import time

from finops import VERSION, report, util, view
from finops.config import Config, ConfigError, load_plans
from finops.ledger import SCHEMA_VERSION
from finops.prices import PriceError, Prices
from finops.scan import run as scan_run

USAGE = """usage: corral-light finops <verb>
  setup [--yes]        accept proposed accounts; type what you pay (once)
  accounts [--json]    the accounts FinOps sees, accepted or proposed
  show [--json]        the dialog's figures as text (or the raw snapshot)
  doctor               how each source is read, and anything wrong
  billing              how to connect an organization's billing API (optional)"""


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return default


def _load_static():
    return load_plans(), Prices.load()


# ── collector ──────────────────────────────────────────────────────────────

def collector_main(argv, env=None, out=None):
    out = out or sys.stdout
    args = list(argv)
    budget = _opt(args, "--budget", "25")
    if args[:1] != ["snapshot"]:
        print("finops collector: expected `snapshot`", file=sys.stderr)
        return 2
    try:
        budget_s = max(1, min(int(budget), 280))
    except ValueError:
        budget_s = 25
    env = env or util.Env.from_environ()
    try:
        plans, prices = _load_static()
        cfg = Config.load(env.config)
        ledger, feed, res = scan_run(env, budget_s)
        try:
            snap = view.build(env, ledger, feed, cfg, plans, prices, scan=res)
        finally:
            ledger.close()
    except (PriceError, ConfigError) as e:
        print(f"finops: {e}", file=sys.stderr)
        return 1
    except Exception as e:                            # noqa: BLE001
        # Type and message only; never a traceback that could quote a line.
        print(f"finops: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return 1
    out.write(json.dumps(snap, separators=(",", ":")) + "\n")
    out.flush()
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────

def cli_main(argv, env=None, out=None, ask=None):
    out = out or sys.stdout
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE, file=out)
        return 0 if args else 2
    verb, rest = args[0], args[1:]
    env = env or util.Env.from_environ()
    try:
        if verb == "setup":
            return setup(env, yes="--yes" in rest, out=out, ask=ask)
        if verb == "accounts":
            return show_accounts(env, as_json="--json" in rest, out=out)
        if verb == "show":
            return show(env, as_json="--json" in rest, out=out)
        if verb == "doctor":
            return doctor(env, out=out)
        if verb == "billing":
            print(BILLING_HELP, file=out)
            return 0
    except (PriceError, ConfigError) as e:
        print(f"finops: {e}", file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2


BILLING_HELP = """Billing APIs (optional; organization accounts with pay-per-call keys only).

Subscriptions (Claude Max, ChatGPT Plus, SuperGrok) have no billing API. If
you also pay for API use through an organization, FinOps can show what that
organization was billed, day by day, beside the rest. It is never added to
Committed. Each vendor needs a key you create once; FinOps never sees it
outside Light's fetch sandbox, which reaches only that vendor's hosts.

  Anthropic  Console > Settings > Admin keys (sk-ant-admin...). Organization
             accounts only. Anthropic documents no read-only admin key: this key
             can manage your organization, so keep it to this one use.
  OpenAI     Platform > Settings > Organization > Admin keys. Owners and
             admins only. OpenAI documents no read-only admin key either.
  xAI        Console > Settings > Management keys, scoped to one team.
  Google     A service account with roles/bigquery.jobUser on the project
             and roles/bigquery.dataViewer on your Cloud Billing export
             dataset; download its JSON key. Needs billing export to BigQuery
             switched on (Billing > Billing export).

Then store the key with Light (it asks for it without echoing it) and grant
it to FinOps for that vendor:

  corral-light module key add anthropic-admin
  corral-light module grant finops anthropic-admin anthropic

  corral-light module key add openai-admin
  corral-light module grant finops openai-admin openai

  corral-light module key add xai-team
  corral-light module grant finops xai-team xai

  corral-light module key add gcp-billing < service-account.json
  corral-light module grant finops gcp-billing gcp \\
      --param table=PROJECT.DATASET.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX

Fetch now with `corral-light module fetch finops`; afterwards it runs every
six hours. Take a key back with `corral-light module revoke finops <key>`."""


def _state(env, budget_s=25):
    plans, prices = _load_static()
    cfg = Config.load(env.config)
    ledger, feed, res = scan_run(env, budget_s)
    return plans, prices, cfg, ledger, feed, res


def show_accounts(env, as_json=False, out=sys.stdout):
    plans, prices, cfg, ledger, feed, _res = _state(env)
    try:
        accts = report.accounts(ledger, feed, cfg, plans)
    finally:
        ledger.close()
    if as_json:
        print(json.dumps([{"id": a["id"], "vendor": a["vendor"], "accepted": a["confirmed"],
                           "plan": a.get("vendor_plan"), "source": a["source"],
                           "committed_cents": a["commit"]["cents"],
                           "committed_kind": a["commit"]["kind"]} for a in accts], indent=2),
              file=out)
        return 0
    if not accts:
        print("No accounts found yet: no usage store or login is in view.", file=out)
    for a in accts:
        c = a["commit"]
        price = (util.usd_cents(c["cents"]) + "/mo " + c["kind"]) if c["cents"] is not None \
            else "no price"
        print(f"{a['id']:20} {a['vendor']:7} {'accepted' if a['confirmed'] else 'PROPOSED':9} "
              f"{(a.get('vendor_plan') or 'plan not stated'):30} {price}", file=out)
        print(f"{'':20} {c['note']}", file=out)
    return 0


def _ask_tty(prompt):
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt).strip()
    except EOFError:
        return None


_MONEY = re.compile(r"^\$?\s*(\d{1,6})(?:\.(\d{1,2}))?$")


def parse_money(text):
    """'100', '$19.99' -> cents; anything else None."""
    m = _MONEY.match((text or "").strip().replace(",", ""))
    if not m:
        return None
    cents = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    return cents if 0 <= cents < 10 ** 8 else None


def setup(env, yes=False, out=sys.stdout, ask=None):
    """Accept proposed accounts, and let the operator type each price once.
    Accepting an account never accepts a price: a catalogue figure is shown
    as a hint and written only as the operator's own typed amount."""
    ask = ask or _ask_tty
    plans, prices, cfg, ledger, feed, _res = _state(env)
    try:
        accts = report.accounts(ledger, feed, cfg, plans)
    finally:
        ledger.close()
    if cfg.error:
        print(cfg.error + "; fix or remove the file, then run setup again.", file=out)
        return 1
    if not accts:
        print("No accounts found yet. Use a lane in Light, then run setup again.", file=out)
        return 0
    interactive = not yes
    if interactive and ask is _ask_tty and not sys.stdin.isatty():
        print("setup needs a terminal to ask about prices; run it in one, or use "
              "`corral-light finops setup --yes` to accept the accounts with no prices.",
              file=out)
        return 1
    today = time.strftime("%Y-%m-%d", time.gmtime(env.now()))
    by_match = cfg.by_match()
    changed = False
    for a in accts:
        c = by_match.get(a["key"])
        if c is None:
            if a["key"].startswith("config:"):
                continue
            print(f"\n{a['id']}: {report.LANE_TITLES.get(a['vendor'], a['vendor'])} account "
                  f"found in {a['source']}; plan {a.get('vendor_plan') or 'not stated'}.",
                  file=out)
            if interactive:
                ans = (ask(f"Accept {a['id']}? [Y/n] ") or "y").lower()
                if not ans.startswith("y"):
                    print("  skipped; it stays proposed.", file=out)
                    continue
            c = {"id": a["id"], "vendor": a["vendor"], "kind": "subscription",
                 "match": a["key"], "accepted_at": today}
            if a.get("vendor_plan"):
                c.update(vendor_plan=a["vendor_plan"], vendor_plan_at=today)
            if a.get("catalogue"):
                c.update(plan=a["catalogue"]["id"], plan_from="catalogue")
            cfg.accounts.append(c)
            changed = True
            print(f"  accepted as {a['id']}" + ("" if interactive else
                                                 " (no price; type one in setup later)"),
                  file=out)
        elif a.get("vendor_plan") and c.get("vendor_plan") != a["vendor_plan"]:
            c.update(vendor_plan=a["vendor_plan"], vendor_plan_at=today)
            if a.get("catalogue"):
                c.update(plan=a["catalogue"]["id"], plan_from="catalogue")
            changed = True
        need_price = c.get("price_from") != "operator" or a["commit"].get("plan_changed")
        if interactive and need_price:
            hint = a["commit"] if a["commit"]["kind"] == "list" else None
            hint_txt = (f" (list price as of {a['catalogue'].get('as_of')}: "
                        f"{util.usd_cents(hint['cents'])}; it is not filled in for you)"
                        if hint and a.get("catalogue") else "")
            while True:
                typed = ask(f"  What do you pay per month for {a['id']}, in dollars?"
                            f"{hint_txt} Enter to skip: ")
                if not typed:
                    print("  no price recorded; it shows as unconfirmed.", file=out)
                    break
                cents = parse_money(typed)
                if cents is None:
                    print("  type an amount like 100 or 19.99", file=out)
                    continue
                c.update(usd_cents_month=cents, price_from="operator", price_at=today,
                         price_for_plan=a.get("vendor_plan") or "")
                if not c["price_for_plan"]:
                    del c["price_for_plan"]
                changed = True
                print(f"  recorded {util.usd_cents(cents)} / month (declared).", file=out)
                break
    if changed:
        cfg.save()
        print(f"\nSaved {cfg.path}.", file=out)
    else:
        print("\nNothing changed.", file=out)
    return 0


def show(env, as_json=False, out=sys.stdout):
    plans, prices, cfg, ledger, feed, res = _state(env)
    try:
        snap = view.build(env, ledger, feed, cfg, plans, prices, scan=res)
    finally:
        ledger.close()
    if as_json:
        print(json.dumps(snap, indent=2), file=out)
        return 0
    print(render_text(snap), file=out)
    return 0


def render_text(snap):
    lines = []
    if snap.get("progress"):
        p = snap["progress"]
        lines.append(f"[{p['phase']} {p['done_pct']}%: {p['note']}]")
    for b in snap["view"]:
        t = b["type"]
        if t == "tiles":
            for it in b["items"]:
                lines.append(f"{it['label']:38} {it['value']:22} [{it['kind']}] {it['note']}")
        elif t == "meter":
            n = int(b["pct"] / 5)
            lines.append(f"{b['label']:38} [{'#' * n}{'.' * (20 - n)}] {b['note']}")
        elif t == "table":
            lines += ["", b["title"]]
            widths = [max(len(str(r[i])) for r in [b["columns"]] + b["rows"] if i < len(r))
                      for i in range(len(b["columns"]))]
            for r in [b["columns"]] + b["rows"]:
                lines.append("  " + "  ".join(str(c).ljust(min(w, 40))
                                              for c, w in zip(r, widths)))
        elif t == "note":
            lines.append("- " + b["text"])
        elif t == "link":
            lines.append(f"{b['label']}: {b['url']}")
    return "\n".join(lines)


def doctor(env, out=sys.stdout):
    """What each source read, how well, and the diagnostics that are not
    figures (vendor vs list, pane cost vs list)."""
    plans, prices, cfg, ledger, feed, res = _state(env)
    p = lambda *a: print(*a, file=out)  # noqa: E731
    try:
        p(f"FinOps {VERSION}, ledger schema {SCHEMA_VERSION}, "
          f"{'sandboxed' if env.sandboxed else 'UNSANDBOXED (acknowledged)'}")
        p(f"Config: {cfg.path or '(none)'}"
          + (f" — {cfg.error}" if cfg.error else f", {len(cfg.accounts)} accounts"))
        p(f"Feed: {'present' if feed.present else 'NOT in view'}")
        for name in ("claude-projects", "codex-sessions", "gemini-store"):
            p(f"Read {name}: {len(env.reads.get(name, []))} folder(s) in view")
        if not res.complete:
            p(f"Backfill: {res.done_pct()}% read, {res.pending_files} files to go")
        for n in res.notes:
            p("Note: " + n)
        p("")
        for src in ("claude", "codex", "gemini"):
            r = ledger.q("SELECT COUNT(*), SUM(lines_ok), SUM(lines_bad), SUM(lines_long) "
                         "FROM files WHERE source=?", (src,))[0]
            n, ok, bad, lng = r[0], r[1] or 0, r[2] or 0, r[3] or 0
            rate = f"{100 * ok / (ok + bad):.1f}%" if ok + bad else "n/a"
            st = ledger.source_state(src)
            p(f"{src:7} files {n:5}  records ok {ok:7,}  bad {bad:5,}  long lines {lng}  "
              f"parse rate {rate}"
              + (f"  FROZEN: {st['note']}" if st and st["state"] == "format_changed" else ""))
        ns = ledger.one("SELECT COUNT(*) FROM grok_session") or 0
        stale = ledger.one("SELECT COUNT(*) FROM grok_session WHERE stale=1") or 0
        p(f"grok    reports {ns}  stale {stale}")
        p("")
        um = {}
        for f in report.facts(ledger, prices, env.now() - 35 * 86400):
            if f.list_micros is None:
                um[f.model or "(no model id)"] = um.get(f.model or "(no model id)", 0) + f.tokens
        if um:
            p("Unpriced models (last 35 days; add a row to data/prices.toml with a source):")
            for m, t in sorted(um.items(), key=lambda kv: -kv[1]):
                p(f"  {m:32} {util.tokens(t)} tokens")
        else:
            p("Every model seen in the last 35 days has a list price.")
        p("")
        _diag_grok(ledger, prices, p)
        _diag_claude_panes(ledger, prices, feed, p)
    finally:
        ledger.close()
    return 0


def _diag_grok(ledger, prices, p):
    """Grok's own cost beside a list estimate of the same turns: a
    diagnostic, never a correction."""
    by = {}
    for f in report.facts(ledger, prices, 0):
        if f.vendor == "grok":
            v = by.setdefault(f.session, [0, 0, 0])
            v[0] += f.vendor_micros or 0
            if f.list_micros is None:
                v[2] += 1
            else:
                v[1] += f.list_micros
    if not by:
        return
    p("Grok: vendor-computed cost vs list estimate (diagnostic only):")
    for sid, (vend, lst, unpriced) in sorted(by.items(), key=lambda kv: -kv[1][0])[:5]:
        p(f"  {sid[:12]}  vendor {util.usd(vend):>10}  list "
          + ("unpriced model" if unpriced else f"{util.usd(lst):>10}"))
    p("")


def _diag_claude_panes(ledger, prices, feed, p):
    """Plan §8.4: for finished Claude panes, the SDK's own cost estimate
    (per process generation) beside the module's list cost."""
    out = []
    by_session = {}
    for f in report.facts(ledger, prices, 0):
        if f.vendor == "claude" and f.session:
            by_session[f.session] = by_session.get(f.session, 0) + (f.list_micros or 0)
    for pane in feed.panes():
        if pane.get("agent") != "claude" or not pane.get("closed"):
            continue
        sid = pane.get("acp_session")
        costs = [u.get("cost", {}).get("amount") for u in pane.get("usage") or []
                 if isinstance(u, dict) and isinstance(u.get("cost"), dict)]
        costs = [c for c in costs if isinstance(c, (int, float))]
        if not sid or not costs or pane.get("segments"):
            continue                       # cleared or resumed: cost was reset
        mine = by_session.get(sid, 0)
        if mine:
            out.append((pane["id"], costs[-1], mine))
    if not out:
        return
    p("Claude panes: the SDK's cost estimate vs FinOps list cost (diagnostic only):")
    for pid, sdk, mine in out[-5:]:
        sdk_m = int(round(sdk * 1e6))
        diff = (mine - sdk_m) * 100 / sdk_m if sdk_m else 0
        p(f"  pane {pid[:8]}  SDK {util.usd(sdk_m):>10}  FinOps {util.usd(mine):>10}  "
          f"({diff:+.0f}%)")
