#!/usr/bin/env python3
"""Extract per-turn read/write events, compaction boundaries and hand-off texts for every trial."""
import json,glob,os,re,sys
OUT=sys.argv[1]
READ=re.compile(r'^\s*(?:sudo\s+)?(cat|head|tail|less|more|nl|bat|sed|awk|grep|rg|wc|strings|xxd|jq|diff|read_doc|open_doc|view_doc|show_doc)\b')
EXEC=re.compile(r'^\s*(?:sudo\s+)?(python3?|node|bash|sh|make|cargo|go|pytest|npm|npx|gcc|g\+\+|coqc|lean|./)')
LIST=re.compile(r'^\s*(?:sudo\s+)?(ls|pwd|tree|find|du|df|which|type|echo|inbox|list_docs)\b')
PATH=re.compile(r'(?<![\w\-=:$])((?:/|\./|~/)?[\w.\-]+(?:/[\w.\-]+)+|[\w.\-]+\.(?:py|txt|json|toml|yaml|yml|md|sh|c|cpp|cc|h|hpp|rs|go|js|ts|tsx|jsx|html|css|cfg|ini|log|csv|tsv|sql|v|lean|tex|conf|xml|env|lock|ipynb|Dockerfile|mk|cmake))')
WRITEOP=re.compile(r'(>>?|\btee\b|sed\s+-i|\bcp\b|\bmv\b|\btouch\b|\bpatch\b|\bgit\s+apply|\bapplypatch\b)')
def paths(seg):
    return [p for p in PATH.findall(seg) if not p.startswith('-') and p not in ('/dev/null','.','..') and not p.startswith('http')]
def classify(cmd):
    reads=set(); writes=set(); kinds=set()
    for seg in re.split(r'\|\||&&|;|\n', cmd):
        seg=seg.strip()
        if not seg: continue
        # heredoc write: cat > file <<EOF
        if WRITEOP.search(seg):
            m=re.search(r'>>?\s*([^\s|;&]+)',seg)
            if m and PATH.fullmatch(m.group(1).strip('"\'')): writes.add(m.group(1).strip('"\''))
            m=re.search(r'sed\s+-i[^ ]*\s+(?:-e\s+)?(?:"[^"]*"|\'[^\']*\'|\S+)\s+(\S+)',seg)
            if m: writes.add(m.group(1).strip('"\''))
            m=re.search(r'\b(?:cp|mv)\s+(?:-\S+\s+)*\S+\s+(\S+)',seg)
            if m: writes.add(m.group(1).strip('"\''))
            m=re.search(r'\btee\s+(?:-a\s+)?(\S+)',seg)
            if m: writes.add(m.group(1).strip('"\''))
        for sub in seg.split('|'):
            sub=sub.strip()
            if READ.match(sub):
                kinds.add('read')
                for p in paths(sub):
                    if p not in writes: reads.add(p)
            elif EXEC.match(sub): kinds.add('exec')
            elif LIST.match(sub): kinds.add('list')
            else: kinds.add('other')
    return reads,writes,kinds
def kind_of(resp):
    if re.search(r'"task_complete"\s*:\s*true',resp): return 'S'
    if re.search(r'"commands"\s*:\s*\[\s*\]',resp): return 'E'
    return 'C'
jobs=sys.argv[2:]
out=[]
for pat in jobs:
    for d in sorted(glob.glob(pat)):
        tr=d+"/agent/trajectory.json"; dec=d+"/agent/optlimit_decisions.jsonl"
        if not (os.path.exists(tr) and os.path.exists(dec)): continue
        job=d.split("/")[-2]; task=os.path.basename(d).split("__")[0]
        T=json.load(open(tr)); ag=[s for s in T["steps"] if s.get("source")=="agent"]
        steps=[]
        for i,s in enumerate(ag):
            ep=f"{d}/agent/episode-{i}/"
            r=open(ep+"response.txt",errors="ignore").read() if os.path.exists(ep+"response.txt") else (s.get("message") or "")
            cmds=[]
            for c in re.findall(r'"keystrokes"\s*:\s*"((?:[^"\\]|\\.)*)"',r):
                try: c=json.loads('"'+c+'"')
                except Exception: pass
                cmds.append(c)
            reads=set(); writes=set(); kinds=set()
            for c in cmds:
                a,b,k=classify(c); reads|=a; writes|=b; kinds|=k
            obs=" ".join((x.get("content") or "") for x in ((s.get("observation") or {}).get("results") or []))
            m=s.get("metrics") or {}
            steps.append(dict(i=i,kind=kind_of(r),cmds=cmds,reads=sorted(reads),writes=sorted(writes),kinds=sorted(kinds),obs_chars=len(obs),prompt=m.get("prompt_tokens"),cache=m.get("cache_tokens"),out=m.get("completion_tokens"),ts=s.get("timestamp")))
        D=[json.loads(l) for l in open(dec)]
        comps=[]
        for o in D:
            if o.get("action")=="summarize":
                t=o["turn_idx"]
                hp=f"{d}/agent/episode-{t}/prompt.txt"
                handoff=open(hp,errors="ignore").read() if os.path.exists(hp) else ""
                comps.append(dict(turn=t,prompt_before=o.get("prompt"),limit=o.get("limit"),handoff_chars=len(handoff),handoff_paths=sorted(set(paths(handoff)))))
        try: reward=float(open(d+"/verifier/reward.txt").read().strip())
        except Exception: reward=None
        cfg=json.load(open(d+"/config.json")) if os.path.exists(d+"/config.json") else {}
        model=(cfg.get("agent") or {}).get("model_name") or ""
        out.append(dict(job=job,task=task,dir=d,model=model,reward=reward,n_steps=len(steps),comps=comps,limits=[o.get("limit") for o in D if o.get("limit")][-1:],steps=steps))
        print(job,task,len(steps),"steps",len(comps),"comps",file=sys.stderr)
json.dump(out,open(OUT,"w"))
print("trials",len(out),"compactions",sum(len(t["comps"]) for t in out))
