# Proof: ZeroDOM on a PortSwigger access-control lab

A reproducible demonstration of ZeroDOM's `compare` catching a broken-access-control
bug. PortSwigger Academy labs are a deliberately-vulnerable training range (free with
an account) — testing your own launched instance is intended use. Keep it polite
(one instance at a time; ZeroDOM's `max_rps` scope + read-only crawler cover the
~1 req/s courtesy).

Labs are **ephemeral** — each launch gets a fresh, time-limited hostname, so there is
no stable link. "Reproducible" here means: launch your own free instance, paste its
host, run `run.sh`, get the same finding. This directory holds:

- `run.sh` — the exact command flow (read-only detection).
- `output.jsonl` — a captured run's machine-readable findings (committed; lab data is
  throwaway, nothing to redact).
- a recording (`demo.cast` / `.gif`) of a live run.

## Hero: "User role controlled by request parameter" (apprentice)

`zerodom compare $LAB/admin --as user=user.json --as admin=admin.json` fetches the
admin panel under two identities and reports the **403-vs-200** delta plus
`only_admin` — the "delete user" controls one identity sees and the other doesn't.
Identity `admin` is wiener's own session with the lab's exploit cookie (`Admin=true`)
added; that edit *is* the exploit, done honestly through a saved storage-state.

See the header of `run.sh` for the step-by-step (capture `user.json`, derive
`admin.json`, set `LAB`, run).

## Honest scope

- This shows **detection**, not the destructive "solved" banner (which requires
  actually deleting a user). ZeroDOM's scope denylist blocks destructive actions by
  default.
- `compare`'s byte-identical *cross-tenant* tell (two real tenant accounts, identical
  private body) isn't in the free apprentice set, which gives one login — that needs a
  self-hosted multi-user app. The 403-vs-200 / `only_admin` branch shown here is the
  apprentice-lab-friendly demonstration of the same capability.
