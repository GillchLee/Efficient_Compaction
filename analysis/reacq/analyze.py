import json,sys,re,collections,statistics as st
D=json.load(open(sys.argv[1]))
def group(t):
    if t["job"].startswith("haiku_tb4"): return "Haiku/TB4 "+("formula" if "formula" in t["job"] else "80K")
    if t["job"].startswith("haiku10"): return "Haiku/LHTB "+("formula" if "formula" in t["job"] else "80K")
    if t["job"].startswith("luna"): return "Luna/LHTB "+("formula" if ("formula" in t["job"] or "_f_" in t["job"]) else "fixed")
    return "other"
K=5
R={}
def cat_of(p):
    n=p.lower(); b=n.rsplit("/",1)[-1]
    if re.search(r'readme|task|instruction|spec|\.md$|prompt',n): return "task/docs"
    if re.search(r'test|spec_|_test|/tests/',n): return "tests"
    if re.search(r'\.(log|out|err)$|/logs?/|output|result|report|\.csv$|\.tsv$|\.json$|\.npz$|\.txt$',n): return "data/results"
    if re.search(r'\.(py|c|cpp|cc|h|hpp|rs|go|js|ts|tsx|jsx|sh|v|lean|tex|sql|html|css)$',n): return "source"
    if re.search(r'\.(toml|yaml|yml|cfg|ini|conf|env|lock|xml|mk|cmake)$|dockerfile|makefile',n): return "config/build"
    return "other"
for g in sorted(set(group(t) for t in D)):
    T=[t for t in D if group(t)==g]
    hazard=collections.defaultdict(lambda:[0,0])   # offset -> [reread reads, total reads]
    base=[0,0]; percomp=[]; tok_re=0; tok_all=0; tok_win=0
    mention=[0,0]; nomention=[0,0]   # [reread, candidates]
    changed=0; unchanged=0; cats=collections.Counter(); catall=collections.Counter()
    rho=[]; orient=collections.Counter(); first_kind=collections.Counter()
    sticky_share=[]; handoff_len=[]; reread_files_total=0
    for t in T:
        S=t["steps"]; comps=[c for c in t["comps"] if c["turn"]<len(S)]
        prev=0; reacq_sets=[]
        allfiles=collections.Counter()
        for s in S:
            for f in s["reads"]: allfiles[f]+=1
        for c in comps:
            tt=c["turn"]; handoff_len.append(c["handoff_chars"]/4)
            last={}; wrote_after={}  
            for j in range(prev,tt):
                for f in S[j]["reads"]: last[f]=j
            win=set(last)
            # per-offset hazard
            post=S[tt:tt+K]; postfiles=set()
            for off,s in enumerate(S[tt:tt+15]):
                for f in s["reads"]:
                    hazard[off][1]+=1; hazard[off][0]+= f in win
            for s in post: postfiles|=set(s["reads"])
            re_files=postfiles&win; reacq_sets.append(re_files); reread_files_total+=len(re_files)
            percomp.append(len(re_files))
            # tokens of re-read observations in first K turns (attribute obs to reads if the turn re-read something)
            for s in post:
                tok_all+=s["obs_chars"]/4
                if set(s["reads"])&win: tok_re+=s["obs_chars"]/4
            tok_win+=sum(s["obs_chars"]/4 for s in S[prev:tt])
            # mention in handoff
            hp=set(c["handoff_paths"]); hpb={p.rsplit("/",1)[-1] for p in hp}
            for f in win:
                m=(f in hp) or (f.rsplit("/",1)[-1] in hpb)
                tgt=mention if m else nomention
                tgt[1]+=1; tgt[0]+= f in re_files
            # changed between last read and re-read?
            for f in re_files:
                w=any(f in S[j]["writes"] or f.rsplit("/",1)[-1] in {x.rsplit("/",1)[-1] for x in S[j]["writes"]} for j in range(last[f],min(len(S),tt+K)))
                changed+=w; unchanged+= not w
                cats[cat_of(f)]+=1
            for f in win: catall[cat_of(f)]+=1
            # rho: turns until first turn with exec/write (progress) after compaction
            r=None
            for off,s in enumerate(S[tt:tt+30]):
                if s["writes"] or ("exec" in s["kinds"]): r=off; break
            rho.append(r if r is not None else 30)
            # first post-compaction turn classification
            s0=S[tt] if tt<len(S) else None
            if s0:
                if s0["kind"]!="C": first_kind["no command"]+=1
                elif set(s0["reads"])&win: first_kind["re-read old file"]+=1
                elif "list" in s0["kinds"] and not s0["reads"] and not s0["writes"]: first_kind["ls/pwd orientation"]+=1
                elif s0["reads"]: first_kind["read new file"]+=1
                elif s0["writes"]: first_kind["write/edit"]+=1
                elif "exec" in s0["kinds"]: first_kind["run/test"]+=1
                else: first_kind["other"]+=1
            prev=tt
        # baseline re-read rate outside post windows: reads of files read earlier in same window
        prev=0
        for c in comps+[dict(turn=len(S))]:
            tt=c["turn"]; seen=set()
            for j in range(prev,tt):
                inpost=any(0<=j-cc["turn"]<K for cc in comps)
                for f in S[j]["reads"]:
                    if not inpost: base[1]+=1; base[0]+= f in seen
                    seen.add(f)
            prev=tt
        # sticky: share of re-acquired (trial,file) pairs re-acquired >=2 times
        cnt=collections.Counter(f for rs in reacq_sets for f in rs)
        if cnt: sticky_share.append(sum(1 for v in cnt.values() if v>=2)/len(cnt))
    n=len(percomp)
    R[g]=dict(trials=len(T),compactions=n,mean_reacq=st.mean(percomp) if percomp else 0,share_with=sum(1 for x in percomp if x>0)/max(1,n),
        hazard={k:(v[0]/v[1] if v[1] else None) for k,v in sorted(hazard.items())},baseline=base[0]/max(1,base[1]),
        reread_turn_tokens_share=tok_re/max(1,tok_all), reread_tokens_per_comp=tok_re/max(1,n), window_tokens_per_comp=tok_win/max(1,n),
        mention_rate=mention[0]/max(1,mention[1]), nomention_rate=nomention[0]/max(1,nomention[1]), mention_n=mention[1], nomention_n=nomention[1],
        unchanged_share=unchanged/max(1,changed+unchanged), cats={k:v/max(1,sum(cats.values())) for k,v in cats.most_common()}, cats_all={k:v/max(1,sum(catall.values())) for k,v in catall.most_common()},
        rho_mean=st.mean(rho) if rho else None, rho_median=st.median(rho) if rho else None, rho_dist={k:v/len(rho) for k,v in sorted(collections.Counter(min(x,5) for x in rho).items())} if rho else {},
        first_kind={k:v/max(1,sum(first_kind.values())) for k,v in first_kind.most_common()}, sticky_share=st.mean(sticky_share) if sticky_share else None, handoff_tokens=st.median(handoff_len) if handoff_len else None)
for g,r in R.items():
    print(f"\n=== {g}: trials {r['trials']} compactions {r['compactions']}")
    print(f" re-acq files/compaction {r['mean_reacq']:.2f}; compactions with >=1 {100*r['share_with']:.0f}%; sticky share {100*(r['sticky_share'] or 0):.0f}%; handoff median {r['handoff_tokens'] or 0:.0f} tok")
    print(" hazard (share of reads that hit a compacted-window file) by offset:", " ".join(f"{k}:{(v or 0)*100:.0f}%" for k,v in r['hazard'].items() if k<12), f"| baseline {100*r['baseline']:.0f}%")
    print(f" tokens: re-read turns = {100*r['reread_turn_tokens_share']:.0f}% of first-{K}-turn observation tokens; {r['reread_tokens_per_comp']:.0f} tok/compaction vs window obs {r['window_tokens_per_comp']:.0f} tok")
    print(f" mentioned in handoff -> re-read rate {100*r['mention_rate']:.0f}% (n={r['mention_n']}); not mentioned -> {100*r['nomention_rate']:.0f}% (n={r['nomention_n']})")
    print(f" re-read of UNCHANGED file: {100*r['unchanged_share']:.0f}%")
    print(" categories re-read:", {k:f"{100*v:.0f}%" for k,v in r['cats'].items()}, "| all window files:", {k:f"{100*v:.0f}%" for k,v in r['cats_all'].items()})
    print(f" rho (turns to first exec/write after compaction): mean {r['rho_mean']:.2f} median {r['rho_median']}; dist {dict((k,f'{100*v:.0f}%') for k,v in r['rho_dist'].items())}")
    print(" first post-compaction turn:", {k:f"{100*v:.0f}%" for k,v in r['first_kind'].items()})
json.dump(R,open(sys.argv[2],"w"),indent=1)
