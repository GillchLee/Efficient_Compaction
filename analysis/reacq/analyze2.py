import json,sys,re,collections,statistics as st
D=json.load(open(sys.argv[1])); K=5
def group(t):
    if t["job"].startswith("haiku_tb4"): return "Haiku/TB4"
    if t["job"].startswith("haiku10"): return "Haiku/LHTB"
    if t["job"].startswith("luna"): return "Luna/LHTB"
def norm(c): return re.sub(r'\s+',' ',c.strip())
out={}
# ---- (a) rho and re-acq vs window length (turns in compacted window), all groups
bins=[(1,8),(9,13),(14,20),(21,30),(31,50),(51,100),(101,10000)]
wl=collections.defaultdict(lambda:collections.defaultdict(list))
rep=collections.defaultdict(lambda:[0,0]); repc=collections.defaultdict(lambda:[0,0])
totals=collections.defaultdict(lambda:collections.Counter())
cmd_top=collections.defaultdict(collections.Counter)
for t in D:
    g=group(t); S=t["steps"]; comps=[c for c in t["comps"] if c["turn"]<len(S)]; prev=0
    totals[g]["turns"]+=len(S); totals[g]["comps"]+=len(comps)
    for c in comps:
        tt=c["turn"]; L=tt-prev
        win=set(); wincmds=set()
        for j in range(prev,tt):
            win|=set(S[j]["reads"]); wincmds|={norm(x) for x in S[j]["cmds"]}
        post=S[tt:tt+K]; postfiles=set().union(*[set(s["reads"]) for s in post]) if post else set()
        re_files=postfiles&win
        r=30
        for off,s in enumerate(S[tt:tt+30]):
            if s["writes"] or "exec" in s["kinds"]: r=off; break
        rr=sum(1 for s in post if set(s["reads"])&win)   # turns in first K that re-read
        totals[g]["reacq_turns"]+=rr; totals[g]["rho_turns"]+=min(r,30); totals[g]["reacq_files"]+=len(re_files)
        for lo,hi in bins:
            if lo<=L<=hi: b=f"{lo}-{hi if hi<10000 else '+'}"; wl[g][b].append((r,len(re_files),rr)); break
        # exact command repeats in first K turns vs control (last K turns of window vs earlier)
        for s in post:
            for x in s["cmds"]:
                rep[g][1]+=1; rep[g][0]+= norm(x) in wincmds
                if norm(x) in wincmds: cmd_top[g][norm(x)[:60]]+=1
        if L>=2*K:
            early={norm(x) for j in range(prev,tt-K) for x in S[j]["cmds"]}
            for j in range(tt-K,tt):
                for x in S[j]["cmds"]: repc[g][1]+=1; repc[g][0]+= norm(x) in early
        prev=tt
print("=== (a) by window length (turns): rho mean | re-acq files/comp | re-acq turns in first 5")
for g in wl:
    print(g)
    for lo,hi in bins:
        b=f"{lo}-{hi if hi<10000 else '+'}"; v=wl[g].get(b)
        if v: print(f"   {b:8} n={len(v):4} rho {st.mean(x[0] for x in v):.2f}  files {st.mean(x[1] for x in v):.2f}  turns {st.mean(x[2] for x in v):.2f}")
print("=== exact command repeats: first-5-turn commands identical to one in compacted window vs in-window control")
for g in rep: print(f"  {g}: post {100*rep[g][0]/max(1,rep[g][1]):.0f}% (n={rep[g][1]}) | control {100*repc[g][0]/max(1,repc[g][1]):.0f}% (n={repc[g][1]})")
print("=== totals: share of all turns spent re-acquiring (turns with re-read in first 5) and rho-turns")
for g,c in totals.items(): print(f"  {g}: turns {c['turns']} comps {c['comps']} re-read turns {c['reacq_turns']} ({100*c['reacq_turns']/c['turns']:.1f}%) rho-turns {c['rho_turns']} ({100*c['rho_turns']/c['turns']:.1f}%)")
print("=== top repeated commands (Haiku/TB4):"); [print("   ",n,c) for c,n in cmd_top["Haiku/TB4"].most_common(8)]
print("=== top repeated commands (Luna/LHTB):"); [print("   ",n,c) for c,n in cmd_top["Luna/LHTB"].most_common(6)]
# ---- (b) Luna hand-off variants
print("=== (b) Luna hand-off variants (LHTB 1.0, same 3 tasks)")
for job in ["luna10c_f_stock","luna10c_f_single","luna10c_f_single2k","luna10c_l25k","luna10c_l40k","luna10c_l15k","luna10_l60k"]:
    T=[t for t in D if t["job"]==job]
    if not T: continue
    files=[]; rhos=[]; hl=[]; ment=[0,0]
    for t in T:
        S=t["steps"]; comps=[c for c in t["comps"] if c["turn"]<len(S)]; prev=0
        for c in comps:
            tt=c["turn"]; win=set().union(*[set(S[j]["reads"]) for j in range(prev,tt)]) if tt>prev else set()
            post=S[tt:tt+K]; pf=set().union(*[set(s["reads"]) for s in post]) if post else set()
            files.append(len(pf&win)); hl.append(c["handoff_chars"]/4)
            r=30
            for off,s in enumerate(S[tt:tt+30]):
                if s["writes"] or "exec" in s["kinds"]: r=off; break
            rhos.append(r)
            hp=set(c["handoff_paths"])|{p.rsplit("/",1)[-1] for p in c["handoff_paths"]}
            for f in pf&win: ment[1]+=1; ment[0]+= (f in hp or f.rsplit("/",1)[-1] in hp)
            prev=tt
    rw=[t["reward"] for t in T if t["reward"] is not None]
    print(f"  {job:20} comps {len(files):4} handoff {st.median(hl) if hl else 0:6.0f} tok | re-acq files {st.mean(files) if files else 0:.2f} | rho {st.mean(rhos) if rhos else 0:.2f} | re-read files mentioned in handoff {100*ment[0]/max(1,ment[1]):.0f}% | reward mean {st.mean(rw) if rw else 0:.2f}")
# ---- handoff length vs re-acq (all)
print("=== hand-off length vs re-acq (all compactions, quartiles of hand-off tokens)")
pairs=[]
for t in D:
    S=t["steps"]; comps=[c for c in t["comps"] if c["turn"]<len(S)]; prev=0
    for c in comps:
        tt=c["turn"]; win=set().union(*[set(S[j]["reads"]) for j in range(prev,tt)]) if tt>prev else set()
        post=S[tt:tt+K]; pf=set().union(*[set(s["reads"]) for s in post]) if post else set()
        pairs.append((c["handoff_chars"]/4,len(pf&win),group(t))); prev=tt
for g in ["Haiku/TB4","Luna/LHTB"]:
    P=sorted(p for p in pairs if p[2]==g and p[0]>0); q=len(P)//4
    print("  ",g, " | ".join(f"Q{i+1} {st.mean(x[0] for x in P[i*q:(i+1)*q]):.0f}tok -> {st.mean(x[1] for x in P[i*q:(i+1)*q]):.2f} files" for i in range(4)))
# ---- (c) strategy coverage (online): at compaction k, which re-reads would be pre-empted
print("=== (c) strategy coverage: share of re-acquired files covered | extra tokens per compaction (last observation of covered files)")
strat=collections.defaultdict(lambda:collections.defaultdict(lambda:[0,0,0.0]))
for t in D:
    g=group(t); S=t["steps"]; comps=[c for c in t["comps"] if c["turn"]<len(S)]; prev=0
    winhist=[]  # list of sets of files read per window
    lastobs={}  # file -> obs tokens of last read turn (approx: obs_chars/4 of that turn / n reads)
    for c in comps:
        tt=c["turn"]; win=set()
        for j in range(prev,tt):
            for f in S[j]["reads"]: win.add(f); lastobs[f]=S[j]["obs_chars"]/4/max(1,len(S[j]["reads"]))
        post=S[tt:tt+K]; pf=set().union(*[set(s["reads"]) for s in post]) if post else set(); re_files=pf&win
        hp=set(c["handoff_paths"])|{p.rsplit("/",1)[-1] for p in c["handoff_paths"]}
        cands={
          "A: files named in hand-off": {f for f in win if f in hp or f.rsplit("/",1)[-1] in hp},
          "B: files read in >=2 windows so far": {f for f in win if sum(f in w for w in winhist)>=1},
          "C: files re-read after an earlier compaction": {f for f in win if any(f in w for w in getattr(t,'_re',[]))} if False else set(),
          "D: last 3 turns kept verbatim": set().union(*[set(S[j]["reads"]) for j in range(max(prev,tt-3),tt)]),
          "E: last 10 turns kept verbatim": set().union(*[set(S[j]["reads"]) for j in range(max(prev,tt-10),tt)]),
          "F: A ∪ B": None,
        }
        cands["F: A ∪ B"]=cands["A: files named in hand-off"]|cands["B: files read in >=2 windows so far"]
        for name,cs in cands.items():
            if name.startswith("C"): continue
            s=strat[g][name]; s[0]+=len(re_files&cs); s[1]+=len(re_files); s[2]+=sum(lastobs.get(f,0) for f in cs)
        winhist.append(win); prev=tt
for g in strat:
    print(" ",g)
    for name,(cov,tot,tok) in strat[g].items():
        n=len([1 for t in D if group(t)==g for c in t["comps"] if c["turn"]<len(t["steps"])])
        print(f"    {name:42} covers {100*cov/max(1,tot):4.0f}% of re-reads | +{tok/max(1,n):6.0f} tok/compaction")
