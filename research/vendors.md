# TTS & STT Vendor Research — Telugu+English Code-Switched Outbound Voice Agent

Research date: **2026-08-27**. Stack: Pipecat, India PSTN outbound, Indian startup.
Targets: **sub-700ms perceived latency**, **under ₹1.5/min all-in**.

Every number is labelled **[vendor claim]**, **[independent benchmark]**, or **[docs]**.
Where a number is not published anywhere I could reach, it says **not published** — nothing is invented.

FX assumption used throughout: **1 USD = ₹88** (Aug 2026). Where a provider prices in INR, the INR
figure is used directly.

STATUS: complete.

---

## Bottom line

**TTS: start on Smallest.ai Lightning v3.1, keep Sarvam Bulbul v3 as the A/B challenger, and keep
Cartesia Sonic-3.5 as the quality ceiling you fall back to if both lose the listening test.**
Smallest.ai is the only vendor that advertises the exact thing this product needs — automatic
language detection with **mid-sentence language switching** — supports Telugu with a dedicated voice
catalogue, publishes **~200 ms TTFB** [vendor claim], and geo-routes to **Hyderabad**, which removes
the ~220 ms India→US round trip that quietly destroys every US-hosted vendor's advertised latency.
Expected: **~200–260 ms TTFB from an Indian server** [vendor claim, unverified in India] at
**≈₹0.48/min of call** *derived*. Sarvam Bulbul v3 is the India-native fallback at sub-250 ms
[vendor claim] and **≈₹0.93/min**. Cartesia is the best-measured model in the set (188 ms P50, 100 ms
IQR [independent benchmark]) and tops both Artificial Analysis speech Elo boards, but costs
**≈₹1.02–1.09/min** and its India code-switch tuning is demonstrably aimed at **Hinglish, not
Tenglish** — its India page never mentions Telugu.

**STT: Sarvam `saaras:v3-realtime`, with ElevenLabs Scribe v2 Realtime as the challenger.** Sarvam
is **₹0.50/min**, ships a named **`codemix` output mode** — the only vendor with one — runs
server-side VAD tuned for Indian acoustic conditions, and has a first-party Pipecat service class
with a published production guide. Budget its finalisation lag at **~600–1,100 ms** (≈576 ms default
VAD silence + a 500 ms client buffering floor on `stream_type="fast"`), tunable down toward ~300 ms
by raising `high_vad_sensitivity` and lowering `silence_duration_ms` [docs]. ElevenLabs Scribe v2 is
the one to test against it, at **₹0.41–0.57/min**, because it exposes **manual commit** — you decide
when a segment finalises, so your own turn-detector drives the clock rather than a vendor silence
timer — and it offers **India data residency**.

**Combined stack cost: ≈₹1.00/min for STT+TTS** (₹0.48 + ₹0.50), leaving roughly **₹0.50/min** for
LLM and telephony inside the ₹1.5 ceiling. The conservative pairing (Sarvam TTS + Sarvam STT) is
**≈₹1.43/min** and leaves essentially nothing — so if you go all-Sarvam, the budget is already gone.

**On sub-700 ms, honestly:** the binding constraint is not TTS. Gladia — the only vendor publishing
finalisation lag with a real methodology — measures **~700 ms median from end-of-speech to final
transcript** on a fast English model. That is the entire budget, spent before the LLM sees a token.
Sub-700 ms perceived latency is reachable only by running an aggressive 250–350 ms endpoint
threshold, or by driving finalisation from your own semantic turn-detector (which is precisely why
ElevenLabs' manual commit matters), or by starting the LLM speculatively on the last stable interim.
Deepgram **Flux** solves this properly with integrated semantic end-of-turn at a **median <300 ms**
[vendor claim] — and **does not support Telugu**. That is the sharpest trade-off in this report.

**Two disqualifications worth knowing up front:** ElevenLabs *Flash v2.5* TTS, Deepgram *Aura-2*,
Rime and Neuphonic have **no Telugu at all**; and on the STT side Deepgram *Flux*, AssemblyAI
*Universal-Streaming*, and NVIDIA *Parakeet/Canary* have **no Telugu at all**. Deepgram *Nova-3*
does support Telugu (`te`) — it is only Flux that doesn't.

---

## Section 1 — TTS

### Cost model used in this section

TTS vendors bill per character; voice agents are budgeted per call-minute. Conversion used:

- Spoken rate ≈ **13 characters/second of generated audio** (~780 chars per minute of audio).
  Telugu is syllabic; ~5 syllables/sec at ~2.5 Unicode codepoints/syllable lands in the same range
  as English at ~150 wpm. This is my assumption, not a vendor figure.
- Agent talk-time ≈ **40% of wall-clock** on an outbound qualification call (the rest is caller
  speech, silence, and dial/ring). So **≈310 billed characters per minute of call**.
- FX: **1 USD = ₹88**.

So **₹/min of call = (price per 1M chars) × 310 / 1,000,000**. Every ₹/min figure below is derived
that way and is marked *derived*. Change the talk-time assumption and every number moves linearly —
at 60% talk-time, multiply by 1.5.

### The independent benchmark that matters

The only continuously-running independent TTS latency benchmark I found with a stated methodology is
**Coval's TTS benchmark**, mirrored at openbenchmarks.com. Published methodology: pinned dataset
`b49649de69e2`, rolling 7-day aggregate, endpoints re-run roughly every 30 minutes, median and P95
time-to-first-audio plus WER computed by re-transcribing the output. Last updated 2026-08-26. It does
**not** publish the client's geographic region, which matters a lot here — these are almost certainly
measured from US infrastructure, so add India→US RTT (~220-250 ms to us-east) if you call a US
endpoint from an Indian server.

Rows I could read [independent benchmark]:

| Model | Provider | TTFA median | TTFA P95 | WER | Samples |
|---|---|---|---|---|---|
| vui | Fluxions | 102 ms | 159 ms | 6.1% | 2,544 |
| palabra-tts-v1 | Palabra | 105 ms | 155 ms | 6.0% | 3,223 |
| inworld-tts-2-flash | Inworld | 108 ms | 171 ms | 4.9% | 3,349 |
| inworld-tts-2 | Inworld | 163 ms | 244 ms | 4.7% | 3,350 |
| eleven_flash_v2_5 | ElevenLabs | 188 ms | 242 ms | 7.2% | 210 |

An earlier snapshot of the same benchmark (captured 2026-05-04, quoted by Gradium) gives
[independent benchmark]: Cartesia **Sonic-3 at 188 ms P50 with a 100 ms IQR**, ElevenLabs Turbo v2.5
264 ms, Flash v2.5 288 ms, Deepgram Aura-2 313 ms P50 / 68 ms IQR. The two snapshots disagree about
Flash v2.5 (188 vs 288 ms) — treat all of these as ±100 ms. And note Cartesia's **100 ms IQR**: its
median is excellent but its spread is the widest of the fast group, which is what a caller actually
feels.

**None of these benchmarks test Telugu.** All TTFA figures below are for English/European text.

### TTS comparison table

| Provider | Model | TTFB / TTFA | Cost | Telugu quality | Streaming | India region | Source |
|---|---|---|---|---|---|---|---|
| Cartesia | Sonic-3.5 / 3.6 | 40 ms Sonic Turbo, "sub-90 ms" Sonic-3.5 [vendor claim]; **188 ms P50, 100 ms IQR** [independent benchmark, Sonic-3, May 2026] | ~$37–39/1M chars → **₹1.02–1.09/min** *derived* | Telugu is one of 42 langs; Sonic-3.6 is #1 on both Artificial Analysis speech Elo boards (1,283 Provider-Voice / 1,123 Controlled-Voice) [third-party arena, vendor-announced]. Telugu-specific MOS **not published** | Yes, WebSocket | **Partly** — Cartesia states it has data centres in India for local deployment, but this reads as an enterprise/on-prem offer, not a public regional endpoint | [cartesia.ai/india](https://www.cartesia.ai/india), [docs](https://docs.cartesia.ai/build-with-cartesia/tts-models/latest) |
| Sarvam AI | Bulbul v3 | "sub-250 ms first byte via WebSocket" [vendor claim]. No independent measurement exists | **₹30 / 10,000 chars** = ₹3,000/1M → **₹0.93/min** *derived* | Purpose-built Indic; Telugu `te-IN`, 35+ voices from professional artists, 11 languages [vendor claim]. Telugu MOS/CMOS **not published** | Yes — REST, HTTP-stream, WebSocket. **Caveat: sample rates above 24 kHz are REST-only, not available in streaming** [docs] | Yes — Indian company, INR billing | [sarvam.ai/blogs/bulbul-v3](https://www.sarvam.ai/blogs/bulbul-v3), [docs](https://docs.sarvam.ai/api-reference-docs/models/bulbul) |
| Smallest.ai | Lightning v3.1 / v3.1 Pro | **~200 ms TTFB** [vendor claim, stated in their own API docs] | ~$0.175/10K chars = $17.5/1M → **₹0.48/min** *derived*. Their agent-pricing page separately quotes "~$0.09/min TTS", which does **not** reconcile with the character rate — verify before committing | Telugu among 15 langs with dedicated voice catalogues; claims **automatic language detection and mid-sentence language switching** [vendor claim] — the only vendor advertising exactly the Tenglish behaviour we need | Yes — HTTP, SSE, WebSocket | **Yes — geo-routed servers incl. Hyderabad** [vendor claim] | [docs.smallest.ai](https://docs.smallest.ai/models/documentation/text-to-speech-lightning/overview), [blog](https://smallest.ai/blog/introducing-lightning-v3) |
| ElevenLabs | Flash v2.5 | ~75 ms [vendor claim]; **188 ms** and **288 ms** P50 in two Coval snapshots [independent benchmark] | $50/1M chars → **₹1.36/min** *derived* | **Telugu is NOT in Flash v2.5's 32 languages** (Tamil and Hindi are). Disqualified | Yes | No | [elevenlabs.io/docs/overview/models](https://elevenlabs.io/docs/overview/models) |
| Deepgram | Aura-2 | sub-200 ms baseline, 90 ms optimised [vendor claim]; **313 ms P50, 68 ms IQR** [independent benchmark] | $30/1M chars | **7 languages (en, es, nl, fr, de, it, ja). No Telugu.** Disqualified | Yes | No | [developers.deepgram.com/docs/tts-models](https://developers.deepgram.com/docs/tts-models) |
| Microsoft | Azure Neural TTS (te-IN) | **not published** by Microsoft | $16/1M prebuilt Neural, $22/1M Neural HD; commitment tiers to ~$7.50/1M → **₹0.44/min** *derived* at $16 | Telugu neural voices have shipped for years — reliable but flat and "IVR-ish" next to the 2026 generation. Code-switching needs explicit SSML `<lang>` tags around English spans, which your LLM must emit | Yes | **Yes — Central India**, and Neural HD voices are now available there | [Azure Speech voice updates](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/azure-speech-%E2%80%93-neural-hd-text-to-speech-recent-voice-updates/4505380) |
| Google | Chirp 3: HD | **not published**. Google ships streaming controls but no ms benchmark | $30/1M chars → **₹0.82/min** *derived*; 1M free chars/month | Telugu `te-IN` confirmed supported; 60+ locales. Telugu naturalness **not independently benchmarked** | Yes — streaming synthesis, but only ALAW / MULAW / OGG_OPUS / PCM | **No India endpoint.** GA regions: `global`, `us`, `eu`, `asia-southeast1`, `europe-west2`, `asia-northeast1`. Singapore is the closest hop from Mumbai | [Chirp 3 HD docs](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd) |
| Rime | Mist v3 / Arcana v3 | sub-100 ms on-prem, sub-200 ms cloud [vendor claim]; **200–350 ms streaming TTFA over WebSocket** [vendor docs]. Coval flags Rime models as high-variance | ~$0.030/audio-min PAYG (~$39/1M chars effective) | **English, Spanish, French, German only. No Telugu.** Disqualified | Yes | No | [docs.rime.ai/docs/models](https://docs.rime.ai/docs/models) |
| Neuphonic | Neu cloud / NeuTTS Air | **not published** in any source I could reach | **not published** | No Telugu in their published language set. Disqualified for this use case | Yes | No | — |
| Gnani.ai | Indian-language TTS | **p95 under 250 ms** [vendor claim] | **not published** — enterprise contract | 10 Indian languages incl. Telugu; claims **MOS 4.23** [vendor claim, no methodology and no per-language breakdown] | Yes — WebSocket + REST | Yes, Indian company | [gnani.ai/text-to-speech-api](https://www.gnani.ai/text-to-speech-api) |
| Reverie | Indic TTS | **not published** | **not published** — enterprise contract | 11+ Indian languages incl. Telugu | **not published** | Yes, Indian company | [reverieinc.com](https://reverieinc.com/products/speech-to-text-api/) |
| CoRover | BharatGPT voice | **not published** | **not published** | 12+ Indian languages claimed; no TTS-specific spec sheet published | **not published** | Yes | — |
| Krutrim | TTS | **not published** | **not published** | **Nothing usable found.** Krutrim publishes no TTS latency, pricing, or Telugu quality data I could reach | **not published** | Yes | — |

### Self-hostable open-weight TTS for Telugu

The honest summary: **most of the fashionable open TTS models do not support Telugu at all.** The
short list that genuinely does is AI4Bharat's.

| Model | Telugu? | VRAM | Realtime factor | Notes |
|---|---|---|---|---|
| **IndicF5** (AI4Bharat) | **Yes** — 11 Indian languages incl. Telugu | **not published** by AI4Bharat. It is an F5-TTS-class DiT, so expect ~6–8 GB in fp16 on one GPU — *inferred from architecture, not measured* | **not published** | Trained on 1,417 h from Rasa, IndicTTS, LIMMITS, IndicVoices-R. **Reference-prompt (voice-cloning) model** — you supply a reference clip per voice, which is fine for a fixed agent persona. The strongest genuinely-Telugu open option. [HF](https://huggingface.co/ai4bharat/IndicF5), [GitHub](https://github.com/AI4Bharat/IndicF5) |
| **Indic Parler-TTS** (AI4Bharat) | **Yes** — 21 Indian languages | **not published**. Parler-TTS mini class → roughly 4–6 GB fp16, *inferred not measured* | **not published**. Parler-TTS is autoregressive and generally slower than F5-class models; treat as batch-grade until you measure it | 1,806 h multilingual training; text-description-controlled voices. [AI4Bharat page](https://ai4bharat.iitm.ac.in/areas/model/TTS/Indic%20Parler%20TTS/) |
| **XTTS-v2** (Coqui) | **No.** 17 languages — Hindi is in, Telugu is not | ~4 GB | ~0.2–0.3 RTF on a good GPU (widely reported, no authoritative benchmark) | Also legally awkward — Coqui Public Model License is non-commercial. Rule out. |
| **F5-TTS** (base) | **No** — English + Chinese only | ~6–8 GB | **not published** | IndicF5 *is* the Telugu-capable fine-tune of this architecture. Use that instead. |
| **Orpheus** (Canopy Labs) | **No Telugu.** English plus a small European/Asian set | ~8–16 GB (3B Llama-backbone class) | Real-time achievable on A100/L40S | Rule out for Telugu. |
| **Chatterbox Multilingual v2/v3** (Resemble) | **No Telugu** in the 23-language list I could verify (Hindi is present) | 0.5B model, ~2–4 GB | Real-time on modest GPUs | Good model, wrong languages. Rule out. |

**VRAM and realtime factor are the weakest part of this section.** Neither AI4Bharat repo publishes a
measured RTF or a memory footprint, and I found no independent throughput benchmark for IndicF5.
If self-hosting is on the table, budget a day to measure RTF on your target GPU before designing
around it — do not plan from the inferred numbers above.

**Economics of self-hosting:** an L4 or A10G on an Indian cloud runs roughly ₹40–70/hour. To beat
Smallest.ai's ₹0.48/min you need to sustain roughly 100–145 call-minutes per hour per GPU — about
2 concurrent calls continuously — which is easy at any real volume, *if* the RTF is good enough.
That "if" is unmeasured. Self-hosting is a phase-2 cost lever, not a launch decision.

### The code-switching problem, specifically

This is where the marketing pages stop being useful.

1. **Sarvam explicitly warns against romanized input.** Their docs state that transliterated input
   such as `"Aapka order confirm ho gaya hai"` *"significantly reduces output quality"*, and that you
   should use native script. For Tenglish this means your LLM must emit Telugu script for Telugu
   words and Latin script for English words, in the same string. That is a constraint on the LLM
   prompt, not a TTS setting. [docs]
2. **Cartesia advertises Hinglish, not Tenglish.** Their India page is entirely about Hindi-English in
   Latin script and never mentions Telugu — even though Telugu is in the model's 42-language list.
   The India-tuned code-switch work has clearly been done for Hindi. Do not assume it transfers.
3. **Smallest.ai is the only vendor advertising the exact capability** — "automatic language detection
   and mid-sentence language switching." That is a vendor claim with no benchmark behind it, but it is
   at least a claim about the right thing.
4. **Azure and Google require you to drive it.** Both handle mixed script correctly only when English
   spans are wrapped in SSML language tags. Doable, but it adds structure to the LLM output and a
   parse step in your Pipecat frame pipeline.

Nobody publishes a code-switch quality benchmark for Telugu. **This has to be settled by your own A/B
listening test on your own script** — 20 real utterances from your call flow, four vendors, blind
rating by two Telugu speakers. That test is worth more than everything above.

---

## Section 2 — STT

### Why "finalisation lag" is the number, and why almost nobody publishes it

Vendors advertise **time-to-first-partial** because it is small and flattering. It does not matter
for a voice agent. What matters is the gap between *the caller stopping talking* and *your LLM
being allowed to start*. That gap is:

```
finalisation lag  =  endpoint silence threshold        (the VAD/semantic wait)
                  +  transcript stabilisation          (turning interims into an immutable final)
                  +  client buffering floor            (some SDKs hold audio in chunks)
```

Gladia is the only vendor I found that publishes this metric honestly with a definition. Their
figures for **Solaria on 3-second English utterances** [vendor claim, but with stated methodology]:
time-to-first-partial ~270 ms P95, but **time from end-of-speech to a final, stable transcript
~700 ms median and ~698 ms P95**. They also state the industry norm: agent stacks default to about
**500 ms of silence**, and they recommend 300–600 ms depending on domain.

Read that again against your target. **A 700 ms perceived-latency budget is roughly the entire
finalisation lag of a good STT alone.** Sub-700 ms end-to-end is only reachable if you either
(a) run an aggressive endpoint threshold in the 200–350 ms range and accept more mid-sentence
cut-offs, or (b) use a model with *semantic* end-of-turn detection that can fire before the silence
timer expires, or (c) start the LLM speculatively on the last stable interim. Vendor "sub-150 ms"
headlines are measuring a different thing entirely.

### STT comparison table

Cost per minute below = published hourly or per-minute rate. STT bills the **whole call duration**,
not just caller speech, so ₹/min here is a direct conversion at ₹88/USD.

| Provider | Model | Finalisation lag | Cost | Telugu quality | Streaming | India region | Source |
|---|---|---|---|---|---|---|---|
| **Sarvam AI** | `saaras:v3-realtime` (beta); `saarika:v2.5` deprecated in favour of `saaras:v3` with `mode="transcribe"` | Server-side VAD, default `negative_frames_count`=18 frames × 32 ms @16 kHz = **~576 ms of silence before end-of-speech**; `high_vad_sensitivity` preset drops the boundary to **~64 ms**. Plus a **500 ms client-side buffering floor** on `stream_type="fast"` (1000 ms on `"balanced"`). "Fast mode guarantees <150 ms time to first token" [vendor claim — that's first token, not final] | **₹30/hour = ₹0.50/min** (₹45/hr with diarisation) [docs]. A separate Sarvam marketing page quotes "₹1.5/min" — the two do not agree; the docs pricing page is the one to trust, but confirm in writing | Purpose-built Indic. Telugu `te-IN` supported. **`mode="codemix"` exists as an explicit output mode** — the only vendor shipping a named code-mix mode. Telugu WER **not published** | Yes — WebSocket, interim transcripts, `vad_signals=true` for speech-start/end events | Yes — Indian company, INR billing. No separate India endpoint; single global `wss://api.sarvam.ai` | [streaming docs](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/streaming-api), [Pipecat guide](https://docs.sarvam.ai/api/integration/pipecat-production-guide), [pricing](https://docs.sarvam.ai/api/getting-started/pricing) |
| **ElevenLabs** | Scribe v2 Realtime | "under 150 ms" [vendor claim] — the blog does **not** say whether that is time-to-partial or time-to-final, so treat it as partial. Crucially it exposes **VAD plus a manual commit**: "full control over when to finalize transcript segments", i.e. you can force finalisation from your own turn-detector instead of waiting on a silence timer. That is the single most useful endpointing feature in this table | $0.28–$0.39/hour depending on source/tier → **₹0.41–0.57/min** | 90+ languages incl. Telugu; 93.5% accuracy across 30 languages on "500 hard samples with background noise" [vendor claim, dataset not named]. Third-party reporting says Scribe v2 correctly transcribes English words in Latin script inside Indic audio including Telugu — exactly the Tenglish behaviour, but I could not confirm this on an ElevenLabs page | Yes — WebSocket | **Yes — EU and India data residency options** [vendor, stated on the launch blog] | [Scribe v2 Realtime launch](https://elevenlabs.io/blog/introducing-scribe-v2-realtime) |
| **Deepgram** | Nova-3 (multilingual + Telugu `te`) | **This is the trap.** `UtteranceEnd` is built on interim results, and Deepgram's own docs say interims are typically emitted **once per second**, so `utterance_end_ms` below 1000 "will not offer any benefits"; they recommend **1000–1500 ms**. `endpointing` (silence-based, `speech_final`) is faster but Deepgram warns it can fail to fire at all in noisy audio. Realistic Telugu finalisation lag: **~1.0–1.5 s** | Nova-3 multilingual streaming: $0.0092/min list, $0.0058/min promo → **₹0.51–0.81/min** | **Telugu `te` IS supported — but only on nova-3, and only in the multilingual configuration.** Telugu WER not published by Deepgram | Yes | No | [models & languages](https://developers.deepgram.com/docs/models-languages-overview), [utterance end](https://developers.deepgram.com/docs/utterance-end), [pricing](https://deepgram.com/pricing) |
| **Deepgram** | **Flux** (`flux-general-multi`) | **Median end-of-turn <300 ms, P95 1.5 s** [vendor claim]; Coval's roundup lists ~260 ms integrated EOT. Best-in-class — semantic end-of-turn, no external VAD needed, claimed to save 200–600 ms vs STT+VAD | $0.0078/min multilingual → ₹0.69/min | **Flux supports English, Spanish, French, German, Hindi, Russian, Portuguese, Japanese, Italian, Dutch. Telugu is NOT among them.** Disqualified — you cannot have Flux's end-of-turn *and* Telugu | Yes | No | [models & languages](https://developers.deepgram.com/docs/models-languages-overview) |
| **AssemblyAI** | Universal-Streaming / Universal-3 Pro Streaming | **~300 ms to an immutable transcript**; endpointing combines **acoustic and semantic** features for turn detection [vendor claim]. Coval's roundup lists 300–600 ms median | **$0.15/hour = ₹0.22/min** — by far the cheapest streaming STT here | **English, Spanish, French, German, Italian, Portuguese only.** No Telugu. Disqualified | Yes | No | [Universal-Streaming](https://www.assemblyai.com/blog/introducing-universal-streaming), [multilingual](https://www.assemblyai.com/blog/multilingual-speech-to-text-api-universal-3-pro) |
| **Microsoft** | Azure Speech STT (te-IN) | `segmentationSilenceTimeoutMs` is configurable **100–5000 ms**, but Microsoft Q&A threads report that **values under 500 ms do not reliably break sentences**. Semantic segmentation is offered as an alternative that keys on sentence-ending punctuation. No published end-to-end finalisation number | **Could not retrieve** — the Azure pricing page renders prices client-side and returned `$-` placeholders. Third-party sources put standard real-time STT near $1/audio hour (**≈₹1.47/min**), which would consume the whole budget on its own. **Verify in the Azure portal before considering** | Telugu te-IN supported and mature. Code-switching is weak — Azure expects one locale per recognition, or you use multi-language auto-detect which adds latency | Yes | **Yes — Central India** | [Azure Speech pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/) |
| **Google** | Chirp 2 (STT v2) | **not published.** No endpointing/EOT latency figure anywhere in Google's docs | ~$0.016/min → **₹1.41/min** | Chirp 2 is documented as strong on Indic languages including Telugu. Caveat: Google's own docs say **language support differs between BatchRecognize and StreamingRecognize** — confirm te-IN is available on streaming in your chosen region before building | Yes — `StreamingRecognizer` supported on Chirp 2 | **No.** Chirp 2 GA regions are `asia-southeast1`, `us-central1`, `europe-west4`. **`asia-south1` is not GA.** Singapore is the nearest hop | [Chirp 2 docs](https://cloud.google.com/speech-to-text/v2/docs/chirp_2-model), [pricing](https://cloud.google.com/speech-to-text/pricing) |
| **Gladia** | Solaria | **~700 ms median / ~698 ms P95 end-of-speech → final** on 3-s English utterances; ~270 ms P95 to first partial [vendor claim with a published, reproducible methodology — the most honest disclosure in this whole report] | **not published** on a public page I could reach | 100+ languages claimed. **Telugu presence not confirmed**, and no Telugu WER published | Yes | No | [measuring latency in STT](https://www.gladia.io/blog/measuring-latency-in-stt) |
| **Speechmatics** | Ursa 2 / Flow | "sub-1 s real-time" [vendor claim] — too coarse to budget against. End-of-utterance silence trigger is configurable; no ms figure published | $0.0537/min per Coval's roundup → **₹4.7/min**. That figure looks like an enterprise/premium tier and I could not confirm it on a Speechmatics page | 55+ languages. **Telugu presence not confirmed** | Yes | No | [Speechmatics comparison](https://www.speechmatics.com/company/articles-and-news/best-speech-to-text-ai-guide-apis-platforms-and-services-compared) |
| **Groq** | whisper-large-v3-turbo | **N/A — no streaming.** Neither OpenAI Whisper nor Groq's hosted Whisper supports real-time streaming transcription. You would have to chunk audio yourself and eat a full round trip per chunk | **$0.04/hour = ₹0.06/min** — 8× cheaper than anything else here | Whisper's Telugu WER is poor relative to Indic-specialised models; Groq quotes ~12% WER as a general multilingual figure, with no Telugu breakdown | **No** | No | [Groq model card](https://console.groq.com/docs/model/whisper-large-v3-turbo) |
| **AI4Bharat** | IndicConformer-600M multilingual (self-hosted) | Depends entirely on your own VAD — you own the endpointing, which is actually an advantage: you can run a 250 ms threshold and a semantic turn-detector | GPU cost only | **All 22 official Indian languages incl. Telugu.** Hybrid CTC + RNNT — **the RNNT head is streaming-capable by construction**, which most open Indic ASR is not. **MIT licence.** Only published WER on the card is Hindi 13.2 on ARTPARK-IISc Vaani-Benchmark-V1.0; **Telugu WER not published** | RNNT decoding supports streaming; the repo does not ship a streaming server | Self-hosted in India | [HF card](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual), [AI4Bharat](https://ai4bharat.iitm.ac.in/areas/model/ASR/IndicConformer/) |
| **AI4Bharat** | IndicWhisper (Vistaar) | Whisper-family — **not streaming**. Batch only | GPU cost only | Achieves the lowest WER on **39 of 59 Vistaar benchmarks**, trained on 10,700 h across 12 Indian languages [published paper, arXiv 2305.15386]. Telugu is in the Vistaar benchmark set. This is the strongest *accuracy* claim for Telugu in this report — but it is a 2023 model | No | Self-hosted | [Vistaar](https://github.com/AI4Bharat/vistaar), [arXiv](https://arxiv.org/pdf/2305.15386) |
| **NVIDIA** | Parakeet-TDT-0.6b-v3 / Canary-1b-v2 | Excellent streaming ASR architecture | GPU cost only | **25 European languages only** (bg, hr, cs, da, nl, en, et, fi, fr, de, el, hu, it, lv, lt, mt, pl, pt, ro, sk, sl, es, sv, ru, uk). **No Telugu, no Indian languages at all.** Disqualified | Yes | Self-hosted | [Parakeet v3 card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) |

### Which providers actually expose explicit endpoint / utterance-end control

Ranked by how much control you get, which is what determines whether sub-700 ms is achievable:

1. **ElevenLabs Scribe v2 Realtime — manual commit.** You decide when a segment finalises. This lets
   you drive finalisation from your own turn-detector (Pipecat's smart-turn, or a semantic
   end-of-turn model) instead of waiting on any vendor timer. Strictly the most flexible.
2. **Self-hosted IndicConformer (RNNT).** You own the whole endpointing stack. Maximum control,
   maximum work.
3. **Deepgram Flux — integrated semantic end-of-turn, median <300 ms.** Best-in-class, and
   **unavailable in Telugu**. Worth knowing about for a future Hindi product.
4. **Sarvam — server-side VAD with live-updatable `threshold`, `min_speech_duration_ms`,
   `silence_duration_ms`,** plus `vad_signals=true` for explicit speech-start/end events. Good
   control, and the Pipecat guide is explicit that you must **not** add a `SileroVADAnalyzer`
   alongside it — Sarvam runs its own VAD server-side and doubling up costs you latency.
5. **AssemblyAI — acoustic + semantic endpointing**, ~300 ms immutable. No Telugu.
6. **Deepgram Nova-3 — `endpointing` and `utterance_end_ms`.** Configurable, but `utterance_end_ms`
   has a hard ~1 s floor because it is built on 1-per-second interim results.
7. **Azure — `segmentationSilenceTimeoutMs` (100–5000 ms) and semantic segmentation.** Configurable
   on paper, unreliable below 500 ms in practice per Microsoft's own Q&A.
8. **Google Chirp 2 — nothing published.** You get whatever their internal endpointer does.

---

## Cheapest viable vs best quality

### TTS

| | Pick | Why | Cost |
|---|---|---|---|
| **Cheapest viable** | **Smallest.ai Lightning v3.1** | ₹0.48/min derived, ~200 ms vendor TTFB, Hyderabad region, and the only vendor advertising mid-sentence language switching. Cheap *and* the best fit on paper — which is why it needs verifying rather than trusting | **₹0.48/min** |
| **Best quality** | **Cartesia Sonic-3.5/3.6** | #1 on both Artificial Analysis speech Elo boards, best independently-measured P50 among Telugu-capable vendors (188 ms), India data centres available | **₹1.02–1.09/min** |
| **Best India-native** | **Sarvam Bulbul v3** | Purpose-built Indic, 35+ professional Telugu-capable voices, INR billing, sub-250 ms claimed | **₹0.93/min** |
| **Cheapest that will definitely work** | **Azure Neural TTS te-IN** | Boring, proven, Central India region, ₹0.44/min. Sounds like an IVR. Use as the fallback leg, not the primary | **₹0.44/min** |
| **Do not use** | ElevenLabs Flash v2.5, Deepgram Aura-2, Rime, Neuphonic | **No Telugu.** Not a quality judgement — the language simply isn't there | — |

### STT

| | Pick | Why | Cost |
|---|---|---|---|
| **Cheapest viable** | **Sarvam `saaras:v3-realtime`** | ₹0.50/min, named `codemix` output mode, server-side VAD tuned for Indian acoustics, first-party Pipecat service class with a published production guide | **₹0.50/min** |
| **Best quality / best control** | **ElevenLabs Scribe v2 Realtime** | Telugu in a 90+ language model, **manual commit** so you own finalisation timing, India data residency | **₹0.41–0.57/min** |
| **Incumbent-safe** | **Deepgram Nova-3 multilingual** | Telugu works, mature Pipecat integration — but the ~1 s `utterance_end_ms` floor is a direct tax on your 700 ms target | **₹0.51–0.81/min** |
| **Do not use** | Deepgram Flux, AssemblyAI, NVIDIA Parakeet/Canary | **No Telugu.** Flux is the painful one — best-in-class end-of-turn, wrong language set | — |
| **Do not use** | Groq whisper-large-v3-turbo | ₹0.06/min is irresistible and irrelevant: **no streaming**. Fine for post-call analysis, useless on the turn | — |

---

## Claims I could not verify

Listed so nobody later mistakes these for settled facts.

1. **Every Telugu quality claim in this report.** Not one vendor publishes a Telugu MOS, CMOS, or
   WER. Cartesia, Sarvam, Smallest, Google, Azure and ElevenLabs all list Telugu as supported and
   none of them publishes a number for it. Every Telugu-quality cell in both tables is
   language-list membership plus a vendor adjective.
2. **Every code-switch claim.** No public benchmark for Telugu↔English mid-sentence switching exists
   from any vendor or any third party. Smallest.ai's "mid-sentence language switching", Sarvam's
   `codemix` mode, and the third-party claim that ElevenLabs Scribe v2 handles Latin-script English
   inside Telugu audio are all unverified.
3. **Smallest.ai's own pricing contradicts itself.** The character rate (~$0.175/10K chars, giving
   ₹0.48/min) and their agent pricing page ("~$0.09/min TTS", giving ~₹7.9/min) are ~16× apart. One
   of them is wrong or measures something different. Get this in writing before committing.
4. **Sarvam's STT pricing contradicts itself.** Docs pricing page says ₹30/hour (₹0.50/min); a
   Sarvam marketing surface says ₹1.5/min. A 3× spread on your single largest STT line item.
5. **Azure's actual prices.** The pricing page renders values client-side and returned `$-`
   placeholders. The ~$1/audio-hour STT figure and the $16/1M-char TTS figure are third-party
   reporting, not Microsoft's page.
6. **Cartesia's "India data centres".** Stated on their India marketing page, with no public
   regional endpoint, no region parameter in the docs, and no stated availability tier. Almost
   certainly enterprise-gated. Treat as a sales conversation, not a feature.
7. **Cartesia's 40 ms / sub-90 ms TTFA.** The only independent measurement of Sonic-3 is 188 ms P50
   with a 100 ms IQR, from a US-based client. Neither figure has anything to say about what an
   Indian server sees.
8. **The Coval benchmark's client region.** Not published. Every TTFA number in this report is
   therefore a *model* latency, not a *your-server-to-their-server* latency. Add India→US RTT if you
   call US endpoints.
9. **The two Coval snapshots disagree** about ElevenLabs Flash v2.5 (188 ms vs 288 ms P50) and I
   could not read the full current table — only the top 5 rows rendered. Cartesia's, Deepgram's and
   Rime's current rows come from a May 2026 snapshot quoted second-hand by a competitor (Gradium),
   which is a conflicted source even though the underlying benchmark is independent.
10. **IndicF5 and Indic Parler-TTS VRAM and realtime factor.** Neither AI4Bharat repo publishes
    either. The GB figures in the open-weights table are inferred from model architecture and are
    explicitly labelled as such. Do not plan capacity from them.
11. **IndicConformer Telugu WER.** The model card publishes exactly one number — Hindi 13.2 on
    ARTPARK-IISc Vaani-Benchmark-V1.0. Telugu is unmeasured on that card.
12. **Gnani.ai's MOS 4.23 and p95 <250 ms.** Vendor claims with no methodology, no dataset, and no
    per-language breakdown. Gnani, Reverie, CoRover and Krutrim all publish essentially no
    engineering data — no latency percentiles, no pricing, no WER. For all four the honest entry is
    "talk to sales and run your own test"; none of them can be evaluated from public information.
13. **Speechmatics' $0.0537/min.** From Coval's roundup, not confirmed on a Speechmatics page, and
    ~9× the next-cheapest option — likely a premium tier being quoted as a headline rate.
14. **Gladia and Speechmatics Telugu support.** Both claim 55–100+ languages; neither confirms
    Telugu on a page I could reach.
15. **ElevenLabs Scribe v2's "under 150 ms".** The launch blog never says whether this is
    time-to-partial or time-to-final. Given every other vendor means partial, assume partial.

### The three things to measure yourself before committing

Public information runs out well before the decision does. In priority order:

1. **A blind Telugu code-switch listening test.** 20 real utterances from your actual call script,
   rendered by Smallest.ai, Sarvam, Cartesia and Azure, rated blind by two Telugu speakers. This
   decides TTS. Nothing above can.
2. **Measured finalisation lag from an Indian server.** Instrument end-of-speech → final transcript
   for Sarvam `saaras:v3-realtime` and ElevenLabs Scribe v2, at 300 ms / 500 ms / 700 ms endpoint
   thresholds, and record P50 *and* P95. Gladia's ~700 ms median for a fast English model is the
   warning: this number, not TTFB, is what eats your budget.
3. **Real ₹/min from a week of real calls.** Every cost figure in this report is derived from an
   assumed 40% talk-time. Measure your actual billed characters and audio-seconds per call, then
   recompute. The TTS column moves the most.
