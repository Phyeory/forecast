# Windows VPS — MT5 bridge for the FTMO REAL account

The FTMO MT5 path is Windows-only (`MetaTrader5` python API). The GitHub
Actions runner never holds MT5 credentials: the MT5 session lives here, on
the VPS, exposed only as a localhost HTTP bridge. The runner connects via an
SSH tunnel (`ssh -L 8765:127.0.0.1:8765`) for each run.

## Layout

| Machine | What runs there |
|---|---|
| Windows VPS | MT5 terminal (logged into the FTMO account) + `mt5_bridge_server.py` on `127.0.0.1:8765` |
| GitHub runner | trader + data feed; tunnel to this host; **no secrets beyond the SSH key** |
| Mac | dashboard/control-plane only |

## Setup

1. Provision a small Windows VPS (2 vCPU / 4 GB, e.g. Contabo/Vultr/AWS t3.medium ~$10–25/mo).
2. Install **MetaTrader 5** (64-bit), open **Options → Community/Python** if ML-blocked, log into the FTMO account (demo **free trial first**), enable autostart + auto-login (Options → Server → save).
3. Copy this folder + `backend/market_live/mt5_bridge_server.py` to the VPS.
4. PowerShell (admin): `Set-ExecutionPolicy -Scope Process Bypass; .\install.ps1 -Server "FTMO-Demo"`
   — creates `C:\mt5bridge\`, installs `MetaTrader5`, registers the `MT5Bridge` scheduled task (at logon).
5. Health check **on the VPS**: `Invoke-RestMethod http://127.0.0.1:8765/health`

## SSH access for the GitHub runner (on your Mac)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/prop_vps_gh -C "prop-sim runner" -N ""
# add the PUBLIC key to the VPS:  (VPS)  .ssh/authorized_keys
```
Then in the **prop-sim** repo (Settings → Secrets → Actions) set:
- `PROP_VPS_HOST`  — the VPS IP
- `PROP_VPS_USER`  — Windows SSH user (`Administrator`)
- `PROP_VPS_SSH_KEY` — the full PRIVATE key text (`cat ~/.ssh/prop_vps_gh`)

Only port 22 needs to face the internet. The bridge stays on 127.0.0.1.

## Rehearsal (before any REAL money)

1. **Tunnel + API proof, zero MT5 risk**: run the bridge in simulate mode on
   the VPS (`python mt5_bridge_server.py --port 8765 --simulate`), then from
   the dashboard press **Prop paper** with `runtime/armed.json` temporarily
   set to real, or locally:
   `python -m market_live.run_live live --config market_live/config/gh_prop.json --feed alpaca --once`
   (runner or Mac through an `ssh -L` tunnel) — the daily workflow itself
   also does the reachability probe and fails loudly if the bridge is down.
2. **Demo order proof**: on the FTMO **free trial** (demo) account run
   `python -m market_live.run_live live --config market_live/config/gh_prop.json --feed alpaca --once --place-test-order`
   — one real bridge round-trip (submit + flatten) against MT5.
3. Only then buy the challenge, repeat step 2 against the challenge login,
   and press **Start REAL** on the dashboard.

## Credentials

- MT5 login/password stay in `C:\mt5bridge\creds.env` on the VPS (created by
  `install.ps1`) — never in any repo, never in GitHub.
- The runner only ever needs the SSH key + the Alpaca paper data keys.

## Failure behavior

If the bridge is unreachable at a scheduled run, `deploy/run_daily.sh` exits
before placing any orders — a clean no-op with a loud red run in Actions
(email alert), and the resume cursor untouched.
