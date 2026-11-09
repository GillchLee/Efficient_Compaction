import json,sys,re,collections,statistics as st,datetime
D=json.load(open(sys.argv[1])); K=5
def group(t):
    if t["job"].startswith("haiku_tb4"): return "Haiku/TB4"
    if t["job"].startswith("haiku10"): return "Haiku/LHTB"
    if t["job"].startswith("luna"): return "Luna/LHTB"
def ts(s): return datetime.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp() if s else None
def is_reacq_turn(s,win):
    if s["kind"]!="C": return True                      # empty / submit-only
    if set(s["reads"])&win and not s["writes"] and "exec" not in s["kinds"]: return True
    if s["kinds"] and set(s["kinds"])<= {"list"}: return True   # pure ls/pwd orientation
    return False
rows=[]; rho2=collections.defaultdict(list); rho2_wl=collections.defaultdict(lambda:collections.defaultdict(list))
secs=collections.defaultdict(lambda:{"re":[], "other":[]})
bins=[(1,8),(9,13),(14,20),(21,30),(31,50),(51,100),(101,10000)]
for t in D:
    g=group(t); S=t["steps"]; comps=[c for c in t["comps"] if c["turn"]<len(S)]; prev=0
    reacq_turns=0; lead=0
    for c in comps:
        tt=c["turn"]; L=tt-prev; win=set().union(*[set(S[j]["reads"]) for j in range(prev,tt)]) if tt>prev else set()
        r=0
        for s in S[tt:tt+30]:
            if is_reacq_turn(s,win): r+=1
            else: break
        rho2[g].append(r); lead+=r
        for lo,hi in bins:
            if lo<=L<=hi: rho2_wl[g][f"{lo}-{hi if hi<10000 else '+'}"].append(r); break
        for j in range(tt,min(len(S),tt+K)):
            if set(S[j]["reads"])&win: reacq_turns+=1
        prev=tt
    # seconds per turn: re-read turns vs others (duration = next ts - this ts)
    for j in range(len(S)-1):
        a,b=ts(S[j]["ts"]),ts(S[j+1]["ts"])
        if a and b and 0<b-a<600:
            inpost=any(0<=j-c["turn"]<K for c in comps)
            win=set()
            if inpost:
                c=max((c for c in comps if c["turn"]<=j),key=lambda c:c["turn"]); p=max([0]+[cc["turn"] for cc in comps if cc["turn"]<c["turn"]])
                win=set().union(*[set(S[k]["reads"]) for k in range(p,c["turn"])]) if c["turn"]>p else set()
            key="re" if (inpost and set(S[j]["reads"])&win) else "other"
            secs[g][key].append(b-a)
    rows.append(dict(g=g,job=t["job"],task=t["task"],reward=t["reward"],turns=len(S),comps=len(comps),reacq_turns=reacq_turns,lead=lead,
                     share=reacq_turns/max(1,len(S)),lead_share=lead/max(1,len(S))))
print("=== rho' = leading post-compaction turns that only re-read/orient/idle before a new action")
for g in rho2:
    v=rho2[g]; print(f"  {g}: mean {st.mean(v):.2f} median {st.median(v)} | 0 turns {100*sum(1 for x in v if x==0)/len(v):.0f}% | >=3 turns {100*sum(1 for x in v if x>=3)/len(v):.0f}%")
    print("     by window length:", " | ".join(f"{b}: {st.mean(v):.2f} (n={len(v)})" for b,v in sorted(rho2_wl[g].items(), key=lambda kv:int(kv[0].split('-')[0]))))
print("=== seconds per turn: re-read turns vs other turns (median)")
for g in secs: print(f"  {g}: re-read {st.median(secs[g]['re']) if secs[g]['re'] else 0:.0f}s (n={len(secs[g]['re'])}) | other {st.median(secs[g]['other']):.0f}s")
print("=== per-trial re-acquisition share vs reward, within task (Luna LHTB, >=4 conditions per task)")
import itertools
def spearman(x,y):
    rx={v:i for i,v in enumerate(sorted(x))}; ry={v:i for i,v in enumerate(sorted(y))}
    n=len(x); d=sum((rx[a]-ry[b])**2 for a,b in zip(x,y)); return 1-6*d/(n*(n*n-1)) if n>2 else None
bytask=collections.defaultdict(list)
for r in rows:
    if r["g"]=="Luna/LHTB" and r["reward"] is not None: bytask[r["task"]].append(r)
agree=0; tot=0
for task,rs in bytask.items():
    if len(rs)<4: continue
    x=[r["lead_share"] for r in rs]; y=[r["reward"] for r in rs]
    rho=spearman(x,y); tot+=1; agree+= (rho or 0)<0
    print(f"  {task:28} n={len(rs)} spearman(lead-share, reward) = {rho:+.2f} | lead-share range {min(x)*100:.1f}%..{max(x)*100:.1f}% reward {min(y):.2f}..{max(y):.2f}")
print(f"  tasks with negative correlation: {agree}/{tot}")
# pooled: standardize within task
xs=[];ys=[]
for task,rs in bytask.items():
    if len(rs)<3: continue
    mx=st.mean(r["lead_share"] for r in rs); my=st.mean(r["reward"] for r in rs)
    for r in rs: xs.append(r["lead_share"]-mx); ys.append(r["reward"]-my)
print(f"  pooled within-task spearman: {spearman(xs,ys):+.2f} (n={len(xs)})")
print("=== TB4 Haiku: solved vs unsolved trials")
for g in ["Haiku/TB4"]:
    R=[r for r in rows if r["g"]==g and r["reward"] is not None]
    for lab,sel in [("solved",lambda r:r["reward"]>=1),("unsolved",lambda r:r["reward"]<1)]:
        v=[r for r in R if sel(r)]
        print(f"  {lab:9} n={len(v):3} comps/trial {st.mean(r['comps'] for r in v):.1f} lead-share {100*st.mean(r['lead_share'] for r in v):.1f}% reacq-turn share {100*st.mean(r['share'] for r in v):.1f}%")
# examples: hand-off names the file, next turn re-reads it
print("=== examples: file named in hand-off, re-read in the first post-compaction turn")
n=0
for t in D:
    if group(t)!="Haiku/TB4": continue
    S=t["steps"]; prev=0
    for c in t["comps"]:
        tt=c["turn"]
        if tt>=len(S): break
        win=set().union(*[set(S[j]["reads"]) for j in range(prev,tt)]); prev=tt
        hp=set(c["handoff_paths"])
        for f in set(S[tt]["reads"])&win&hp:
            ep=f"{t['dir']}/agent/episode-{tt}/prompt.txt"; txt=open(ep,errors="ignore").read()
            i=txt.find(f); snippet=txt[max(0,i-150):i+200].replace("\n"," ")
            print(f"  [{t['task']} comp@{tt}] handoff says: ...{snippet}...\n     next command: {S[tt]['cmds'][0][:100]!r} (obs {S[tt]['obs_chars']//4} tok)")
            n+=1; break
        if n>=4: break
    if n>=4: break
json.dump(rows,open(sys.argv[2],"w"))
