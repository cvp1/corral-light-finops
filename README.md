# corral-light-finops

What your Corral Light lanes cost, and how much quota is left.

This is the first out-of-tree module for
[Corral Light](https://github.com/cvp1/corral-light). It is installed
beside Light, never inside it, and runs in Light's module sandbox: it
reads only the usage stores it declares and Light's redacted feed, has no
network, and writes only its own data folder.

```
corral-light module add finops     # type the name to confirm
corral-light finops setup          # accept the accounts it found; type what you pay
```

FinOps needs no setup to start. On its first run it proposes one account
per login it finds and shows their usage and quota at once. `setup`
accepts them and asks, once, what you pay for each. `setup --yes`
accepts every account and records no prices.

## What it shows

| Figure | Kind | Where it comes from |
|---|---|---|
| **Committed** per month | `declared` when you typed it; else `list`, shown apart | your config; else the catalogue price for the plan the vendor states (`data/plans.toml`), marked unconfirmed |
| **Quota** windows | `vendor` | Claude rate-limit notices (through Light's feed); Codex `rate_limits` in its rollouts |
| **Grok cost** this month | `vendor` | the Grok CLI's own `grok usage`, run by Light in its sandbox; not a bill |
| **API-equivalent list cost** this month | `list` | local token counts priced at API list prices (`data/prices.toml`); not a bill, never added to a plan |

Then a table by account, the state of each source, and the panes, consult
panels and worktrees that used the most this week.

```
corral-light finops show        # the dialog as text
corral-light finops accounts    # accepted and proposed accounts
corral-light finops doctor      # parse rates, unpriced models, diagnostics
```

### Notices in Light's rail

On a Light that supports module notices, FinOps puts a short card in the
Needs-you rail when something needs a decision from you:

- a quota window at 75% or more, or that the vendor marks as near its
  limit (`warn`); at 90%, or when the vendor has cut you off (`bad`);
- a source whose record format changed, so its figures are frozen until
  a module update.

The levels are the same as the quota tiles'. A card clears when the
window resets or the condition goes away, and Light clears it if FinOps
stops reporting. Cards never block an agent and never pop the rail open
on a phone. To turn them off, add this line at the top of FinOps's
`config.toml`:

```
notices = "off"
```

This version declares notices in its manifest, which older Lights refuse:
update Light before you update FinOps.

## How the figures are made

- **Null is unreported, never zero.** A figure with no source says
  "unreported". A model with no list price is counted as "unpriced" and
  never priced at $0.
- **Metrics stay apart.** Quota, subscription commitment, vendor-computed
  cost and API-equivalent list cost are different measurements. None is
  added to or corrected by another. Grok's vendor cost and a list
  estimate of the same turns are shown side by side.
- **A catalogue price is never yours until you type it.** Accepting an
  account never accepts a price. When the vendor later reports a
  different plan, the line says "plan changed" and falls back to the
  list price until you type the new amount.
- **Deduplication by the vendor's own record id.** Claude: one record per
  `requestId`, the largest output count wins (records are written once
  per content block). Codex: one record per `response_id`; older
  rollouts without them use the cumulative counter, sorted, with resets
  starting a new baseline. Grok: per session and turn; a forked
  session's turns that ended at or before `forked_at` belong to its
  parent. Gemini: per conversation and step.
- **Same history, same totals.** Every figure is computed at read time
  from the ledger and the price list, so files read in any order, read
  twice, or cut off by a crash give identical totals. Tests check this.
- **Quota freshness.** A window whose reset time has passed shows "reset
  since last report". An observation older than the window's own length
  shows "stale". One with no reset time goes stale after the vendor's
  shortest window. A stale "limit reached" is never shown as current.
- **List prices** are integer micro-dollars per million tokens, effective
  dated, one source URL per row. Cached input is priced at the cache-read
  rate; a cache price a vendor does not list falls back to the input
  price, never to zero. Long-context rows apply per request. Grok turns
  span many model calls, so their estimate always uses the base tier.
- **Format drift.** If fewer than 90% of a source's new records parse in
  a run (at least 20 of them), that source freezes at its last figures
  and says so, until a new FinOps version is installed.
- **Accounts.** Codex accounts are a salted hash of the account id in the
  rollouts. Claude transcripts do not name an account, so Claude usage
  belongs to the login Light reports, from the moment FinOps first saw
  it. Grok and Gemini have one local account each.
- **This host only.** Month boundaries are in the timezone in your config
  (default: the host's).

## Privacy

FinOps reads Claude transcripts, Codex rollouts and Antigravity's
conversation store to count tokens. It keeps only counts, model ids,
session ids and times: no prompt or answer text, no pane titles (those
live only in the current snapshot), and no raw account id. It never
opens a credential file; the plan comes from Light's feed. On Linux the
sandbox makes this enforceable: there is no network and the only
writable place is its data folder. Tests plant sentinel strings in every
source and check they reach neither the ledger nor any output.

## Updating prices

`data/prices.toml` and `data/plans.toml` are plain files in this
repository. Each row names its source page and the day it was checked.
`corral-light finops doctor` lists models seen with no price. A price
change is a new row with a `from` date, so earlier days keep their price.

## Development

Stdlib Python 3.9 or newer; no dependencies.

```
cd tests && python3 -m unittest discover
```

`tests/light_contract.py` is Light's manifest and snapshot validator,
copied verbatim and pinned to a Light commit; every snapshot the tests
build must pass it. `finops/tomlmini.py` is Light's TOML subset reader,
so the config this module writes is one Light can read.

## License

MIT, see [LICENSE](LICENSE).
