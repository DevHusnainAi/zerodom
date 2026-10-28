import json, sys, statistics as st, os
import tiktoken
enc = tiktoken.get_encoding("cl100k_base"); tk = lambda s: len(enc.encode(s))
CAP = sys.argv[1]           # cap100 dir (Stagehand trees)
ZD = sys.argv[2]            # zerodom jsonl
sites = [l.strip() for l in open(sys.argv[3]) if l.strip() and not l.startswith("#")]
ACT = ("link","button","textbox","checkbox","combobox","radio"," tab"," menuitem","searchbox","slider","switch")
zd = {r["i"]: r for r in (json.loads(l) for l in open(ZD))}
WALL = 5
rows = []
for i, url in enumerate(sites):
    tree = open(os.path.join(CAP, f"{i}.txt")).read() if os.path.exists(os.path.join(CAP, f"{i}.txt")) else ""
    sh_tok = tk(tree) if tree.strip() else 0
    sh_act = sum(1 for l in tree.split("\n") if any(r in l for r in ACT))
    z = zd.get(i, {})
    zd_tok, zd_act = z.get("tokens", 0), z.get("nodes", 0)
    rows.append((url, sh_tok, zd_tok, sh_act, zd_act))

# Honest classification — don't fold ZeroDOM's own render failures into "bot-walled".
#   comparable     : both rendered a real page -> fair token/coverage comparison
#   zerodom_failed : Stagehand rendered but ZeroDOM returned ~nothing -> counts AGAINST us
#   stagehand_failed: ZeroDOM rendered but Stagehand returned ~nothing -> against them
#   both_walled    : neither got a page (genuine bot/consent wall) -> excluded, fair
comp = [r for r in rows if r[1] > 50 and r[4] >= WALL and r[3] >= WALL]
zerodom_failed  = [r for r in rows if r[3] >= WALL and r[4] < WALL]
stagehand_failed = [r for r in rows if r[4] >= WALL and r[3] < WALL]
both_walled = [r for r in rows if r[3] < WALL and r[4] < WALL]
print(f"sites: {len(rows)}")
print(f"  comparable (both rendered):  {len(comp)}")
print(f"  ZeroDOM FAILED (SH rendered, ZD ~empty) — counts against us: {len(zerodom_failed)}")
for r in zerodom_failed: print(f"      SH_act={r[3]:4} ZD_nodes={r[4]:3}  {r[0]}")
print(f"  Stagehand failed (ZD rendered, SH ~empty): {len(stagehand_failed)}")
print(f"  both bot-walled (fair exclude): {len(both_walled)}")

sav = [100*(r[1]-r[2])/r[1] for r in comp if r[1]]
print("\n== TOKENS (Stagehand observe tree vs ZeroDOM snapshot) ==")
print(f"  median Stagehand {int(st.median(r[1] for r in comp))}  ZeroDOM {int(st.median(r[2] for r in comp))}")
print(f"  median savings {st.median(sav):.0f}%   mean {st.mean(sav):.0f}%   aggregate {100*(1-sum(r[2] for r in comp)/sum(r[1] for r in comp)):.0f}%")
print(f"  ZeroDOM cheaper on {sum(1 for s in sav if s>0)}/{len(sav)} comparable sites")

print("\n== COVERAGE (actionable elements found) ==")
ratios = [r[4]/r[3] for r in comp if r[3]]
within = sum(1 for x in ratios if 0.8 <= x <= 1.25)
print(f"  median actionable: Stagehand {int(st.median(r[3] for r in comp))}  ZeroDOM {int(st.median(r[4] for r in comp))}")
print(f"  within +/-25% (parity): {within}/{len(ratios)}  ({100*within/len(ratios):.0f}%)")
print(f"  ZeroDOM found >=25% MORE: {sum(1 for x in ratios if x>1.25)}; >=25% FEWER: {sum(1 for x in ratios if x<0.8)}")

# distribution of savings
buckets = {"<0% (worse)":0, "0-40%":0, "40-60%":0, "60-80%":0, ">80%":0}
for s in sav:
    if s < 0: buckets["<0% (worse)"] += 1
    elif s < 40: buckets["0-40%"] += 1
    elif s < 60: buckets["40-60%"] += 1
    elif s < 80: buckets["60-80%"] += 1
    else: buckets[">80%"] += 1
print("\n== savings distribution ==")
for k, v in buckets.items(): print(f"  {k:<14} {v}")
