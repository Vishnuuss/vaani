"""Do the Reference rows fire on the utterances that actually failed?

Offline, free, and the only honest way to know before a billed call. Every case
below is a real line from runs 981/982/983/985 (18 Sep) -- the calls where the
client said the agents "did not answer the questions".

    python tools/verify_reference.py

Exits non-zero if a row stops firing, starts firing on a plain answer, or a TODO
row acquires a TRIGGER and goes live before its fact was filled in.
"""
import sys
sys.path.insert(0,'C:/Users/vishnu/Downloads/dograh-vapi')
sys.stdout.reconfigure(encoding='utf-8',errors='replace')
from pathlib import Path
from api.services.vaani import client_reference as cr

CASES = {
 'wf4_solar': [
   ("ఆహ్, పెట్టిస్తాం గానీ, అంటే సబ్సిడీ అన్నారు. ఇంక నేనేం ఇన్వెస్ట్మెంట్ చేయనవసరం లేదు కదా, అంటే అంతా సబ్సిడీ వస్తదిగా, మొత్తం.", True),
   ("సబ్సిడీ వస్తదా, రాదా మాకు?", True),
   ("సోలార్ గురించా? ఎందుకు?", True),
   ("మీరు ఎక్కడి నుంచి కాల్ చేస్తున్నారు?", True),
   ("వర్షాకాలంలో పని చేస్తుందా?", True),
   ("మాది అపార్ట్మెంట్ అండి", True),
   ("సోలారా? సోలార్ వల్ల ఉపయోగాలు ఏందండి?", True),   # run 798
   ("అంటే సబ్సిడీ అంటున్నారు, ఎంత వస్తుంది సబ్సిడీ?", True),  # run 798
   ("మీరెవరండి?", True),                              # run 400
   ("ఆహ్, మైలు సొంతదే.", False),          # a plain answer must NOT fire a row
   ("సరే.", False),
 ],
 'wf6_property': [
   ("అవును, రెసిడెన్షియల్ ప్లాట్ అంటే ఏంటి? ఇప్పుడు.", True),
   ("అవును, మీరు ఎక్కడి నుంచి కాల్ చేస్తున్నారు?", True),
   ("నా బడ్జెట్ ఎందుకు కావాలి?", True),
   ("ప్లాట్ మీద లోన్ వస్తుందా?", True),
   ("ఎవరండీ మీరు?", True),                            # run 329
   ("ఏం ప్రాపర్టీ?", True),                            # run 579
   ("అదే, హైదరాబాద్.", False),
   ("ఆ, నేను ప్లాట్ చూస్తున్నాను.", False),
 ],
 'wf3_loan': [
   ("మీ దగ్గర ఏమేం లోన్స్ ఉన్నాయి అని అడుగుతున్నాను నేను.", True),
   ("బిజినెస్ లోన్ అంటే ఏంటి?", True),
   ("మీరు నిజంగా HDFC నుంచేనా?", True),
   ("ఇవన్నీ ఎందుకు అడుగుతున్నారు?", True),
   ("ఎడ్యుకేషన్ లోన్ ఉందా?", True),                    # run 399
   ("ఎవరు మీరు?", True),                              # run 976
   ("ఓకే. బిజినెస్ లోన్.", False),
 ],
 'wf5_invest': [
   ("ఇప్పుడు ఎందుకు, ఎందుకు అడుగుతున్నారు ఇప్పుడు?", True),
   ("ఏం చెప్తారు మీ అడ్వైజర్?", True),
   ("నా డబ్బు సేఫ్ గా ఉంటుందా? గ్యారంటీ ఉందా?", True),
   ("ఇన్వెస్ట్మెంట్ అంటే ఏంటి?", True),
   ("అది ఏం ఇన్వెస్ట్ అవుతది?", True),                  # run 800
   ("మీ పేరండి మీరు?", True),                          # run 800
   ("ఏం దేని గురించి?", True),                         # run 578
   ("నాకు పిల్లలు లేరు.", False),
   ("ఇన్వెస్ట్మెంట్ ప్రస్తుతానికి ఏం చేయట్లే.", False),
 ],
}

bad = 0
for name, cases in CASES.items():
    text = Path(f'clients/reference/{name}.md').read_text(encoding='utf-8')
    operational, reference = cr.split(text)
    rows = cr.parse(reference)
    live = [r for r in rows if r.pattern is not None]
    todo = [r for r in rows if r.pattern is None]
    print(f"\n===== {name}: {len(live)} live rows, {len(todo)} inert TODO rows")
    print(f"      compiled-every-turn part of this file: {len(operational.strip())} chars (want 0)")
    if operational.strip():
        print("      !! text before '## Reference' would be added to EVERY turn"); bad += 1
    for utt, want in cases:
        got = cr.lookup(rows, utt)
        ok = bool(got) == want
        if not ok: bad += 1
        title = got[0].split(':')[0].replace('REFERENCE (','').rstrip(')') if got else '-'
        print(f"   {'ok ' if ok else 'FAIL'}  fired={bool(got)!s:<5} want={want!s:<5} [{title[:38]}]  {utt[:52]}")
    for r in todo:
        if r.pattern is not None:
            print(f"      !! TODO row is LIVE: {r.title}"); bad += 1

print(f"\n{'ALL GOOD' if not bad else str(bad)+' PROBLEM(S)'}")
sys.exit(1 if bad else 0)
