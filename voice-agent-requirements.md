# AI Voice Calling Agent: Requirements Analysis

Date: 18 September 2026
Status: Draft for review

---

## 1. Project summary

Build a system that places automated outbound phone calls in which an AI agent holds a natural, real-time, two-way conversation with the person who answers. The agent must listen, understand, respond in a human-quality voice (ElevenLabs), remember the conversation, take actions during the call (look up data, book, record outcomes), and hand off to a human when needed.

The agent is a **reusable engine**: the same platform runs sales follow-up, order/booking confirmation, customer support callbacks, and any future campaign by changing the script, goal, knowledge, and tools per campaign rather than rebuilding.

### Decisions already made

| Area | Decision |
|---|---|
| Direction | Outbound campaigns first. Inbound is a later phase using the same brain. |
| Use cases | All: sales and lead follow-up, order/booking confirmation, customer support, general configurable campaigns. |
| Voice | ElevenLabs. |
| Languages | Hindi, Hinglish (code-switched), English, regional Indian languages (Tamil, Telugu, Marathi, Bengali and others as needed). |
| Build approach | Undecided. Section 8 compares both options in detail. |

---

## 2. What "as smart as a human" means, concretely

A caller judges the agent on a handful of behaviours. Each maps to a hard technical requirement.

| Human behaviour | Technical requirement |
|---|---|
| Understands me even when I mix Hindi and English, have an accent, or speak over background noise | Streaming speech-to-text (STT) with proven Hinglish and regional-language accuracy |
| Replies without awkward pauses | End-to-end response latency under about 800 ms from when I stop speaking to when it starts speaking |
| Stops talking when I interrupt it | Barge-in: voice activity detection plus instant cancellation of the audio being played |
| Knows when I have actually finished a sentence versus just pausing | Semantic turn detection, not a fixed silence timer |
| Remembers what I said two minutes ago and can actually do things | LLM with full conversation context, campaign knowledge, and tool calling into your systems |
| Sounds like a real person | ElevenLabs low-latency streaming voice, sentence-level streaming so speech starts before the full reply is generated |
| Knows when to give up and get a human | Handoff rules and warm transfer to a human agent or a callback ticket |

---

## 3. The voice agent pipeline

Every voice agent, platform or custom, is the same six stages. Audio flows down and back up.

```
Phone network  <->  Telephony (number, carrier, audio stream)
                        |
                        v
               Speech-to-text (streaming, partial results)
                        |
                        v
               Turn detection + interruption handling
                        |
                        v
               LLM brain (prompt, memory, tools, campaign goal)
                        |
                        v
               Text-to-speech (ElevenLabs streaming)
                        |
                        v
               Orchestration + data (campaigns, contacts, outcomes, dashboard)
```

### Latency budget

Target: under 800 ms from end of caller speech to first agent audio. Above about 1.2 s callers start talking over the agent or hanging up.

| Stage | Budget |
|---|---|
| STT final transcript after end of speech | 150 to 300 ms |
| Turn detection decision | 50 to 200 ms |
| LLM first token | 200 to 400 ms |
| TTS first audio byte (ElevenLabs Flash) | 75 to 150 ms |
| Network and telephony | 100 to 200 ms |

Latency is why server location matters. For Indian calls the STT, LLM and TTS should run from an India or Singapore region, and ElevenLabs requires an India-region deployment for Indian traffic (arranged through their sales team).

---

## 4. Functional requirements

### 4.1 Campaign management
- Create a campaign with: name, goal, script or conversation guide, persona and voice, language settings, allowed calling hours, retry policy, and success criteria.
- Campaign types as templates: sales follow-up, order/booking confirmation, support callback, survey, reminder, custom.
- Attach knowledge to a campaign: product info, FAQs, offers, policies. The agent answers from this and does not invent facts.
- Attach tools to a campaign: which systems the agent may read or write during the call.
- Pause, resume, stop, and clone campaigns.

### 4.2 Contacts and lists
- Upload contacts via CSV and via API. Fields: phone, name, preferred language, custom variables used in the script (order ID, appointment time, amount due, etc.).
- Validate numbers, remove duplicates, mark DND-registered numbers and consent status before dialing.
- Segment lists and assign them to campaigns.

### 4.3 Dialing engine
- Schedule calls within allowed hours and the contact's time zone.
- Control concurrency (how many calls at once) per campaign and globally.
- Detect no-answer, busy, voicemail, wrong number, and answering machines. Do not run the script into a voicemail.
- Retry policy: number of attempts, spacing, and which outcomes trigger a retry.
- Priority and throttling so one campaign cannot starve others.

### 4.4 Conversation engine (the brain)
- Opening: greet by name, identify the business, disclose that this is an AI assistant, state the purpose, and ask for a moment of the person's time.
- Language handling: start in the contact's preferred language, detect and switch languages mid-call if the person switches, handle Hinglish naturally.
- Goal-driven dialogue: the agent works towards the campaign goal (confirm, qualify, book, resolve) while handling objections, questions, and digressions.
- Structured data collection: capture answers into defined fields (confirmed yes/no, new time slot, reason for cancellation, lead score).
- Tool calling mid-call: fetch order status, check slot availability, create a booking, update CRM, send a follow-up SMS or WhatsApp message.
- Memory: full context within a call; optional cross-call memory of previous interactions with the same contact.
- Guardrails: do not discuss out-of-scope topics, do not make promises outside policy, do not collect payment card details or OTPs by voice.
- Escalation: transfer to a human if the person asks, is angry, or the agent is stuck after N attempts; otherwise create a callback ticket.
- Closing: summarise, confirm next step, end politely, hang up.

### 4.5 Human handoff
- Warm transfer to a human agent's number or queue with a whispered summary.
- If no human is available: promise a callback and create the task.
- Full transcript and captured fields visible to the human at handoff.

### 4.6 Post-call processing
- Store recording (if consented), full transcript, and detected language.
- LLM-generated call summary, outcome tag, sentiment, and extracted fields.
- Trigger follow-up actions: SMS or WhatsApp confirmation, CRM update, webhook to your systems, add to a retry queue.

### 4.7 Dashboard and operations
- Live view: calls in progress, queue depth, concurrency.
- Campaign analytics: connect rate, completion rate, goal conversion, average duration, cost per call, outcome breakdown.
- Call review: listen to recordings, read transcripts, flag failures, tag issues.
- Prompt and script versioning with the ability to A/B test two versions of an agent on the same list.
- Alerts: error rate spikes, carrier failures, spend thresholds.

### 4.8 Integrations
- CRM and order systems (whatever you use today; to be specified).
- WhatsApp and SMS for confirmations.
- Calendar or booking system for slot management.
- Webhooks in and out so other systems can trigger calls and receive results.

### 4.9 Admin and security
- Roles: admin, campaign manager, reviewer, read-only.
- API keys and secrets stored in a vault, not in code.
- Audit log of who changed scripts, lists, and settings.

---

## 5. Non-functional requirements

| Requirement | Target |
|---|---|
| Response latency | Under 800 ms median, under 1.2 s at the 95th percentile |
| Concurrency | Define launch and 12-month targets (for example 20 concurrent calls at launch, 200 later). Drives carrier plan, ElevenLabs tier, and servers. |
| Availability | 99.5 percent during calling hours for the first version |
| Call setup failure rate | Under 2 percent excluding carrier rejects |
| STT accuracy | Word error rate under 15 percent on your own recorded Hinglish test set |
| Data residency | Recordings and transcripts stored in India |
| Cost visibility | Per-call and per-campaign cost computed automatically |
| Observability | Per-call trace of every stage's latency, plus error logs |
| Testability | Automated test calls against a simulated caller before any campaign goes live |

---

## 6. Language requirements and their consequences

Language is the highest technical risk in this project, more than the voice.

- **Text-to-speech is the solved part.** ElevenLabs supports Hindi, Tamil, Telugu, Marathi, Bengali and other Indian languages, with Flash v2.5 at roughly 75 ms latency for real-time use.
- **Speech-to-text for Hinglish and regional languages is the weak link.** Callers code-switch mid-sentence, speak fast, and call from noisy places. Options:
  - Sarvam AI (Saarika / Saaras): built for Indian languages and code-mixing, streaming with under 150 ms first-token latency, about Rs 1.5 per minute.
  - Deepgram Nova-3: global model, Hindi with code-switching support, cheaper (around $0.005 to $0.008 per minute), but community reports of Hindi-English errors.
  - ElevenLabs Scribe: bundled in the ElevenLabs Agents platform. Hindi supported; regional and code-switch quality must be tested.
- **The LLM must be prompted per language.** Claude handles Hindi, Hinglish, and major Indian languages well, but the system prompt must specify script (Devanagari vs romanised Hinglish for TTS), tone, and formality per campaign. Romanised Hinglish often produces more natural ElevenLabs output than Devanagari for code-mixed speech; this must be tested per voice.
- **Requirement:** before choosing a stack, build a 200-clip test set of real call audio across your target languages and measure each STT option on it. Whichever build approach wins, this test set is needed.

---

## 7. Compliance and legal (India)

This is a hard gate for outbound campaigns. Non-compliance leads to number disconnection and fines up to Rs 10 lakh.

- **TRAI TCCCPR**: promotional calls must go from 140-series numbers; transactional and service calls from 160-series. Register headers and content templates on the DLT platform. TRAI has amendments requiring businesses to declare automated calling systems to their telecom provider.
- **DND**: scrub every list against the NCPR/DND registry before dialing promotional calls. Service calls to customers with a transaction or enquiry in the last 90 days are permitted (implied consent) but keep evidence of the consent.
- **Consent records**: store when and how each contact consented, and honour opt-out requests made during the call immediately ("do not call me again" must update the contact record).
- **AI disclosure**: the agent must state it is an AI assistant at the start of the call.
- **Recording consent**: announce recording, or make recording configurable per campaign.
- **DPDP Act**: personal data (phone numbers, transcripts, recordings) needs a retention policy, deletion on request, and India storage.
- **Calling hours**: enforce TRAI hours (typically 9 am to 9 pm) in the dialing engine, not just in policy.
- **Never collect by voice**: card numbers, CVV, OTPs, passwords.

---

## 8. Build approach comparison

### Option A: ElevenLabs Agents platform

ElevenLabs runs STT, turn detection, interruption handling, the LLM call, and TTS as one hosted service, with phone numbers via Twilio, Plivo, Exotel (added June 2026, including an outbound-call endpoint), or any SIP trunk. You bring the LLM choice (Claude is supported, or a custom LLM endpoint), the prompt, the knowledge base, and tools defined as webhooks into your backend. You build: the campaign manager, dialer, contact lists, compliance scrubbing, tool webhooks, post-call processing, and dashboard.

### Option B: Custom pipeline

You run your own orchestrator (Pipecat or LiveKit Agents), stream audio from Plivo or Exotel or Twilio into it, call an STT vendor of your choice (Sarvam, Deepgram, or ElevenLabs Scribe), call Claude for the brain, and call ElevenLabs streaming TTS for the voice. You build everything in Option A plus the real-time audio layer, turn detection tuning, interruption handling, and latency engineering.

### Detailed comparison

| Dimension | A: ElevenLabs Agents platform | B: Custom pipeline |
|---|---|---|
| Time to first working call | 1 to 2 weeks | 4 to 8 weeks |
| Time to production-quality MVP | 6 to 8 weeks (mostly your backend and dashboard) | 12 to 16 weeks |
| Engineering effort | Backend + dashboard only | Backend + dashboard + real-time audio + tuning |
| Team needed | 1 backend dev, 1 frontend dev, 1 prompt/conversation designer | Add 1 engineer experienced with real-time audio or WebRTC |
| Latency | Tuned by ElevenLabs, typically good | Depends on your engineering; can be excellent, can be poor |
| Interruption and turn detection | Built in | You configure and tune (frameworks ship defaults) |
| STT choice | ElevenLabs Scribe only (verify whether custom STT is allowed) | Any vendor; Sarvam for Indian languages is a real advantage |
| Hinglish and regional accuracy | Must be tested; you cannot swap if weak | You pick the best vendor per language |
| LLM choice | Claude supported natively, or custom LLM endpoint | Claude directly via SDK with full control (caching, tools, effort) |
| Voice | ElevenLabs, native | ElevenLabs via streaming API |
| Telephony (India) | Exotel, Plivo, Twilio, SIP; India deployment via sales | Exotel or Plivo directly; you own the integration |
| Vendor lock-in | High for the conversation layer; low if business logic stays in your backend | Low; every component swappable |
| Debuggability | Platform logs and transcripts; limited visibility into internals | Full per-stage traces |
| Scaling | ElevenLabs handles concurrency; you pay tiers | You scale servers and manage concurrency |
| Data control | Audio transits ElevenLabs infrastructure | Audio transits only your chosen vendors |
| Cost per minute (estimate, Indian calls) | About Rs 9 to 11 | About Rs 6 to 9 |
| Cost at 10,000 minutes per month | About Rs 90,000 to 110,000 | About Rs 60,000 to 90,000 plus hosting (Rs 10,000 to 20,000) |
| Ongoing maintenance | Low | Medium to high |
| Main risk | Language quality you cannot fix; pricing changes | Engineering risk; latency regressions |

### Cost per minute breakdown (estimates, verify with vendor quotes)

| Component | A: Platform | B: Custom |
|---|---|---|
| ElevenLabs Agents per minute | about $0.08 (Rs 7) | not applicable |
| ElevenLabs TTS (agent speaks roughly half the call) | included | about Rs 2 to 4 |
| STT | included | Sarvam Rs 1.5; Deepgram about Rs 0.6 |
| LLM (Claude, with prompt caching) | Rs 1 to 2 | Rs 1 to 2 |
| Telephony (Exotel or Plivo India outbound) | Rs 0.7 to 1.0 | Rs 0.7 to 1.0 |
| Hosting and orchestration | your backend only | your backend plus real-time servers |
| Total | Rs 9 to 11 | Rs 6 to 9 |

LLM estimate assumes Claude Opus 5 at $5 per million input and $25 per million output tokens, a 3-minute call with about 10 turns, a cached system prompt, and short spoken replies. Claude Sonnet 5 roughly halves this.

The cost gap is small. The real difference is **control over language quality versus speed to market**.

### Recommendation

Run a **two-week pilot on Option A** before committing. It is the cheapest way to learn what the product needs to be, and the Hinglish and regional-language quality question can only be answered by testing, not by reading.

Build the pilot so switching later is cheap:
- Keep every piece of business logic (campaign config, contacts, tools, outcomes, compliance) in your own backend. The ElevenLabs agent only holds the prompt and calls your webhooks.
- Build the language test set (section 6) during the pilot and score ElevenLabs STT on it. In parallel, score Sarvam on the same clips.
- Decision gate at the end of the pilot: if ElevenLabs STT accuracy on your Hinglish and regional clips meets the bar and the latency is acceptable, continue on Option A for launch. If not, move the conversation layer to Option B (Pipecat + Sarvam + Claude + ElevenLabs TTS) and keep the backend you already built.

If you already know regional languages are a day-one requirement for most call volume, go straight to Option B with Sarvam STT, because that is where Indian-language accuracy currently leads.

---

## 9. Target architecture

Shown for the recommended path; the custom path replaces the "ElevenLabs Agents" box with your own real-time server.

```
                 +------------------------------+
                 |  Dashboard (web app)         |
                 |  campaigns, lists, analytics |
                 +---------------+--------------+
                                 |
                 +---------------v--------------+
                 |  Backend API (your server)   |
                 |  - campaign + contact store  |
                 |  - dialer + scheduler        |
                 |  - DND/consent scrubbing     |
                 |  - tool webhooks             |
                 |  - post-call processing      |
                 |  - cost + analytics          |
                 +------+---------------+-------+
                        |               |
        start call      |               |  tool calls, call events
                        v               |
        +---------------+-------+       |
        |  ElevenLabs Agents    |<------+
        |  STT, turn-taking,    |
        |  LLM call, TTS        |
        +-----------+-----------+
                    |  SIP / native integration
        +-----------v-----------+
        |  Telephony (Exotel or |
        |  Plivo; 140/160 nos.) |
        +-----------+-----------+
                    |
                 Caller

Data: Postgres (campaigns, contacts, calls, outcomes), object storage in India
      (recordings), queue (dial jobs, retries), CRM/WhatsApp/booking integrations.
```

### The brain

- Model: Claude Opus 5 (`claude-opus-5`) as the default; Claude Sonnet 5 for cost-sensitive high-volume campaigns after measuring quality.
- Run at low or medium effort for conversational turns to keep latency down; use higher effort only for post-call summarisation, which is not latency-sensitive.
- One stable, cached system prompt per campaign: persona, language rules, goal, knowledge, guardrails, disclosure text. Per-contact variables go after the cached prefix.
- Tools defined with strict JSON schemas: `get_order`, `check_slots`, `book_slot`, `update_crm`, `send_whatsapp`, `transfer_to_human`, `end_call`.
- Post-call: a second Claude call produces summary, outcome tag, sentiment, and extracted fields as structured output.

---

## 10. Vendor shortlist

| Role | Shortlist | Notes |
|---|---|---|
| Telephony (India) | Exotel, Plivo | Both integrate with ElevenLabs natively and support 140/160 series. Twilio is 2 to 3 times the cost for India. |
| Voice (TTS) | ElevenLabs Flash v2.5 or current real-time model | Decided. |
| STT | Sarvam (Indian languages), Deepgram Nova-3, ElevenLabs Scribe | Choose by test-set score. |
| LLM | Claude Opus 5, Claude Sonnet 5 | Via ElevenLabs agent config or directly via the Anthropic SDK. |
| Orchestration (custom path only) | Pipecat, LiveKit Agents | Pipecat for provider flexibility; LiveKit for scale and native SIP. |
| Data | Postgres, S3-compatible storage in an India region, Redis or a queue for dial jobs | |
| Backend language | Python (best library support for Pipecat and voice tooling) or TypeScript | To be decided. |

---

## 11. Phased roadmap

### Phase 0: Discovery and pilot (weeks 1 to 2)
- Finalise campaign list and one pilot campaign (recommend order or booking confirmation: simplest, highest value, lowest compliance risk since it is transactional).
- Obtain a 160-series number and an Exotel or Plivo account. Start DLT registration.
- Build the language test set from real recordings.
- Configure an ElevenLabs agent with a Claude brain and one tool webhook. Make 50 test calls.
- Score STT accuracy, latency, and caller reactions. Decision gate on approach.

### Phase 1: MVP (weeks 3 to 8)
- Backend: campaigns, contacts, dialer with concurrency and retries, DND scrubbing, consent records, calling-hour enforcement.
- Conversation: pilot campaign script hardened, disclosure, handoff to a callback ticket, opt-out handling.
- Post-call: transcript, recording, summary, outcome, WhatsApp confirmation.
- Dashboard: campaign list, upload contacts, live calls, outcomes, call review.
- Automated test-caller harness for regression testing scripts.
- Launch the pilot campaign at low concurrency.

### Phase 2: Multi-campaign platform (weeks 9 to 14)
- Campaign templates for sales follow-up and support callback.
- Warm transfer to human agents.
- CRM and booking integrations.
- A/B testing of scripts and voices.
- Cost and analytics reporting.
- Second and third languages added and tested.

### Phase 3: Scale and inbound (weeks 15 onward)
- Inbound number routed to the same agent.
- Cross-call memory per contact.
- Higher concurrency tiers; multi-carrier failover.
- Move to the custom pipeline if the decision gate or later needs call for it.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Hinglish and regional STT accuracy below the bar | Agent misunderstands callers; campaign fails | Test set before committing; Sarvam fallback; custom path |
| Latency above 1 s on Indian networks | Callers talk over the agent or hang up | India-region deployment; per-stage latency tracing; Flash TTS; low LLM effort |
| Regulatory violation | Number disconnection, fines | Compliance built into the dialer, not left to process; DLT and DND done before launch |
| Callers reject AI callers | Low completion rate | Honest disclosure, natural voice, quick opt-out, human handoff |
| Agent invents facts or over-promises | Customer harm, brand damage | Knowledge-grounded prompts, guardrails, review loop, structured outcomes |
| Vendor pricing or API changes | Cost or rework | Business logic in your backend; swappable components |
| Voicemail and IVR detection failures | Wasted minutes and cost | Answering-machine detection from the carrier plus agent-side detection |

---

## 13. Open questions to close before build

1. Which systems must the agent read and write during calls (CRM, order database, booking calendar)? What APIs do they have?
2. Expected call volume: calls per day at launch and in 12 months, and peak concurrency?
3. Which campaign goes first? (Recommendation: order or booking confirmation.)
4. Do you have existing call recordings to build the language test set, and which regional languages carry real volume?
5. Do you have a human team to receive handoffs, and during which hours?
6. Are you already registered on DLT, and do you have 140 or 160 series numbers?
7. Backend language preference: Python or TypeScript?
8. Budget ceiling per call minute and per month?
9. Should recordings be kept, for how long, and who may access them?
10. Will the same platform serve multiple brands or businesses (multi-tenant) or one?

---

## 14. Immediate next steps

1. Answer the open questions in section 13.
2. Sign up for ElevenLabs (business tier), Exotel or Plivo, and the Anthropic API; request India-region deployment from ElevenLabs.
3. Collect 200 real call clips across target languages for the STT test set.
4. Start DLT registration and number procurement, since this has the longest lead time.
5. Run the two-week pilot described in Phase 0 and hold the approach decision gate.

---

## Sources consulted

- ElevenLabs SIP trunking: https://elevenlabs.io/docs/eleven-agents/phone-numbers/sip-trunking
- ElevenLabs Twilio native integration: https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/native-integration
- ElevenLabs Plivo integration: https://elevenlabs.io/agents/integrations/plivo
- ElevenLabs India: https://elevenlabs.io/india
- ElevenLabs changelog, Exotel support (June 2026): https://elevenlabs.io/docs/changelog/2026/6/1
- ElevenLabs Agents pricing: https://elevenlabs.io/pricing/agents
- ElevenLabs models: https://elevenlabs.io/docs/overview/models
- Sarvam AI speech to text: https://www.sarvam.ai/speech-to-text and pricing https://docs.sarvam.ai/api/getting-started/pricing
- Deepgram Nova-3 multilingual: https://deepgram.com/learn/nova-3-multilingual-major-wer-improvements-across-languages
- Deepgram Hindi-English discussion: https://github.com/orgs/deepgram/discussions/1208
- TRAI compliance guides: https://frejun.com/trai-compliance-call-centers/ and https://www.caller.digital/blog/trai-dnd-compliance-ai-outbound-calling-india
- Business Standard on TRAI automated-call amendments: https://www.business-standard.com/technology/tech-news/ai-calls-how-to-identify-legitimate-calls-spam-scams-126091500686_1.html
- Plivo India voice pricing: https://www.plivo.com/voice/pricing/in/
- Telephony comparison for India: https://caller.digital/blog/telephony-partner-voice-ai-india-plivo-exotel-ozonetel-knowlarity-twilio-2026
- Pipecat vs LiveKit: https://soniox.com/wiki/voice-agent-frameworks
