"""Cumulative cost at reward saturation per trial.

    python analysis/saturation_cost.py JOB_DIR... > out.csv

sat = first submission whose interim score equals the trial's best (interim_verifier.jsonl /
phase_rewards*.jsonl via token_turns submissions.csv).  Jobs without interim scores fall back
to the first submission time parsed from trial.log (src=first-submit; score unknown) or, for
a passed trial, its end.  cost_* are cumulative $ from turns.csv up to that time."""
import csv, bisect, json, re, sys
from pathlib import Path
from datetime import datetime
def ts(s): return datetime.fromisoformat(s).timestamp()
rows=[]
for j in sys.argv[1:]:
    j=Path(j); tt=j/"token_turns"
    if not (tt/"turns.csv").exists(): continue
    turns=list(csv.DictReader(open(tt/"turns.csv")))
    subs=list(csv.DictReader(open(tt/"submissions.csv"))) if (tt/"submissions.csv").exists() else []
    summ={r["trial"]:r for r in csv.DictReader(open(tt/"summary.csv"))}
    for trial in sorted({r["trial"] for r in turns}):
        tr=[r for r in turns if r["trial"]==trial]; s=summ[trial]
        t0=min(ts(r["timestamp"]) for r in tr)
        ev=sorted((ts(r["timestamp"])-t0,float(r["cost_usd"])) for r in tr)
        times=[e[0] for e in ev]; cum=[]; c=0
        for e in ev: c+=e[1]; cum.append(c)
        cost_at=lambda sec:(cum[bisect.bisect_right(times,sec)-1] if bisect.bisect_right(times,sec) else 0.0)
        budget=None
        try: budget=json.load(open(j/trial/"config.json"))["agent"]["override_timeout_sec"]
        except Exception: pass
        sb=[r for r in subs if r["trial"]==trial and r.get("reward") not in ("",None)]
        src=""; sat=None; satrew=None; n_sub=len(sb)
        if sb:
            src="score"
            best=max(float(r["reward"]) for r in sb)
            for r in sorted(sb,key=lambda r:ts(r["timestamp"])):
                if float(r["reward"])>=best-1e-9:
                    sat=ts(r["timestamp"])-t0; satrew=best; break
        else:
            log=j/trial/"trial.log"
            rem=[float(m.group(1)) for m in re.finditer(r"\((\d+(?:\.\d+)?)s remaining",log.read_text())] if log.exists() else []
            n_sub=len(rem)+(1 if s["end"]!="timeout" else 0)
            if rem and budget:
                src="first-submit"; sat=budget-rem[0]; satrew=None
            elif s["end"]=="passed":
                src="passed"; sat=times[-1]; satrew=float(s["reward"])
        rows.append(dict(job=j.name,task=s["task"][:24],model=s["model"],cond=s["condition"][:12],reward=s["reward"],end=s["end"],
            n_sub=n_sub,src=src,sat_min=round(sat/60,1) if sat is not None else "",sat_rew=satrew,cost_sat=round(cost_at(sat),4) if sat is not None else "",
            cost_total=round(cum[-1],4),dur_min=round(times[-1]/60)))
w=csv.DictWriter(sys.stdout,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
