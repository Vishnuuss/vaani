# 34 — The bookish Telugu is a re-ask artefact

**11 September 2026**

---

## The complaint

*"the way it speaks telugu sentences is not good... make it efficient on Telugu
spoken words and fillers and how to handle objections... not bookish telugu."*

The complaint is correct. The diagnosis it implies — that the Telugu prompt is
badly written — is not.

---

## What was measured

Every line the agent spoke across MB Solar runs 857–861 (2026-09-10): **49 agent
turns, 39 distinct sentences.** Not the prompt's intentions — what the customer
actually heard.

### It is not speaking Telangana Telugu

| Marker | Count in 49 turns |
|---|---|
| Telangana forms (-తది, -తరు, ఉంటరు, అయితది) | **0** |
| Standard/coastal forms (-తుంది, -తారు, ఉంటున్నారు) | 5 |

### The bookish sentences customers heard

| Spoken to a customer | Register |
|---|---|
| ఏ ప్రాంతం లేదా నగరంలో **నివసిస్తున్నారు**? | "do you reside" — written |
| ఏ రకమైన ప్రాపర్టీ **కలిగి ఉన్నారు**? | "do you possess" — translationese |
| సైట్ సర్వే **చేయించుకోవాలనుకుంటున్నారా**? | twelve syllables, unsayable |
| మీ పేరు **చెప్పగలరా**? | "are you able to state" |
| ఏ సమయం **సౌకర్యంగా ఉంటుంది**? | written |
| **ఇబ్బంది పెట్టినందుకు క్షమించండి** | news-reader Telugu |

It also contradicts itself: "ఏరియా / సిటీ" in some turns, "ప్రాంతం / నగరం" in
others; "కమర్షియల్ ప్లేస్" and "కమర్షియల్ స్థలం" both.

---

## Where those words come from — the actual finding

Of the ten worst bookish forms, **only two are written anywhere in any prompt
layer.** The other eight appear in no layer at all. The model invents them.

And the Layer 3 question texts, written by hand, are **good spoken Telugu**:

```
property_type      మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?
monthly_bill       మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?
location           మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?
roof_available     మీకు సొంత రూఫ్ లేదా టెర్రస్ ఉందా?
```

So the prompt says "ఏరియా" and the agent said "ప్రాంతం". Why?

### Similarity of each ask to its own written text, by ask number

| Run / field | ask 1 | ask 2 | ask 3 |
|---|---|---|---|
| 861 assessment_agreed | **100%** | 63% | 38% |
| 859 roof_available | **100%** | 90% | — |
| 861 property_type | **96%** | 75% | 75% |
| 859 monthly_bill | **90%** | 51% | — |
| 859 location | 77% | 69% | **62%** |
| 858 monthly_bill | 71% | 53% | **46%** |

**The first ask is nearly always verbatim. Every re-ask drifts — and the drift
direction is always toward written Telugu.**

Run 859's location question degrades 77% → 69% → 62%, and the third attempt is
the one that says "నివసిస్తున్నారు". Run 861's property question is the one that
introduces "స్థలం". Run 861's survey question is the one that produces
"చేయించుకోవాలనుకుంటున్నారా".

---

## The causal chain

1. Turn-taking fires before the caller has finished → the agent re-asks.
2. Layer 1 forbids saying the same sentence twice in a call.
3. So a re-ask **must** be a paraphrase.
4. Nothing anchors that paraphrase to spoken register, so it falls back to the
   model's default Telugu — which is bookish, because Telugu training text *is*
   written Telugu.

The anti-repetition rule and the broken turn-taking combine to manufacture
bookish Telugu. Neither is a prompt-quality problem.

---

## What follows

**Rewriting the Telugu prompt would fix almost nothing.** The prompt's Telugu is
already right and the agent already says it correctly on the first attempt. The
bookish Telugu is downstream of the re-asking. Fix the turn-taking and most of
it disappears without a single word being rewritten.

Three things are worth doing, in this order:

### 1. Fix the re-asking (not a prompt change)
`semantic_turn_completion` → on, `turn_start_min_words` 1 → 3. Covered in doc 33.

### 2. Anchor the rephrase instead of rewriting it
Give every question two or three **pre-written** spoken variants, so a forced
rephrase picks from a list rather than inventing:

```
location
  1. మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?
  2. ఏ ఏరియా అండి?
  3. సిటీలో ఎక్కడ ఉంటున్నారు?

monthly_bill
  1. మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?
  2. నెలకి బిల్లు ఎంత వస్తుంది అండి?
  3. సుమారుగా ఎంత అవుతుంది — వెయ్యి, రెండు వేలు, ఇలా?

assessment_agreed
  1. ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?
  2. ఫ్రీ site survey పెట్టుకుందామా అండి?
  3. ఒకసారి వచ్చి చూస్తారా? ఖర్చు ఏమీ లేదు.
```

**NEVER invent a new wording for a question. Rephrase only from this list.**

### 3. Fix the two bookish lines that were genuinely written by hand

| Now | Replace with |
|---|---|
| మీ పేరు **చెప్పగలరా**? | మీ పేరు ఏంటి అండి? |
| **ఇబ్బంది పెట్టినందుకు క్షమించండి** | సారీ అండి, డిస్టర్బ్ చేసినందుకు |

---

## Dialect decision

Asked and answered on 11 Sep: **neutral spoken Telugu, not Telangana.**

MB Solar's office is in Vijayawada (Andhra) while the leads in runs 859 and 861
were in Hyderabad. Committing the whole script to Telangana forms would mark the
agent as an outsider to every Andhra lead, and the rule on dialect is
all-or-nothing — scattered regional forms inside otherwise neutral Telugu sound
like someone imitating an accent.

Neutral spoken Telugu is native-sounding in both states, and it is where ~90% of
the "sounds robotic" problem lives anyway.

---

## Two claims NOT made here

- **Nothing has been changed in any prompt.** This is an audit.
- **The "duplicate prompts" theory did not survive.** All 170 substantive lines
  of the Layer 3 editor text were compared against Layers 1, 2 and 4: **two**
  overlap. There is nothing meaningful to delete, and the prompt is not bloated
  by Telugu script either (Telugu is 4–24% of each layer; the layers already use
  English instructions with Telugu examples).

  The one real duplicate is instructive. *"Never say the same sentence twice in
  a call"* is written in **both** Layer 1 and Layer 3 — and the agent repeated
  itself verbatim in four of five calls. Writing a rule twice does not make it
  obeyed.
