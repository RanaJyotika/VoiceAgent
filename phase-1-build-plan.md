# Phase 1 Build Plan: Abandoned Cart and Order Confirmation Calling

Date: 18 September 2026
Companion to: voice-agent-requirements.md
Primary goal: lowest cost per call minute that still sounds good and converts.

---

## 1. Phase 1 scope

Two campaigns, one engine, one dashboard.

| Campaign | Trigger | Goal of the call | Outcome fields |
|---|---|---|---|
| Order confirmation | Shopify `orders/create` webhook (or GoKwik order event) | Confirm the order, address and delivery slot; catch wrong orders and COD fakes early | confirmed / cancelled / changed, new address, new slot, notes |
| Abandoned cart recovery | GoKwik abandoned checkout webhook, or Shopify `checkouts/create` with no matching order after a delay | Remind the customer of the item, explain benefits, answer questions, offer to complete the order, send the checkout link on WhatsApp | interested / not interested / call later, objection, link sent, order placed |

Note on the store name: the platform is **GoKwik** (the one-click checkout used on Indian Shopify stores), not "Gowick". GoKwik does not offer self-serve webhooks. You ask your GoKwik point of contact to add an abandoned-cart webhook pointing at our URL. Until that is set up, we use Shopify's own `checkouts/create` and `checkouts/update` webhooks, which we can enable ourselves.

### Which one to build first

**Order confirmation first.** Reasons:

- The trigger is a standard Shopify webhook we control. No dependency on GoKwik's team.
- The conversation is short and structured: confirm, maybe change, done. Easy to script, easy to measure.
- It is a transactional call to a customer who just bought. It goes from a 160-series number and has no DND or promotional-consent problem.
- The UI is small: a list of orders called, outcome per order, a play button for the recording, and a settings page. Everything built here is reused for abandoned cart.

Abandoned cart is second because it adds the GoKwik dependency, the timing logic (when to call), the WhatsApp link step, and a sales-style conversation that needs more prompt work and more testing. It is also closer to a promotional call, so the number series and consent handling need care (section 7).

### Timing for abandoned cart calls

Do not call the instant an item is added to cart. Most people are still shopping. Recommended rule: call 20 to 45 minutes after checkout was started and not completed, only within calling hours, and only once per cart. Add-to-cart without reaching checkout gives you no phone number anyway; the phone number only exists once the customer starts checkout on GoKwik or Shopify. Make the delay a campaign setting so you can test 15, 30 and 60 minutes.

---

## 2. Analysis of the video

Video: "Build a real-time voice AI agent with Google ADK and Gemini Live API", Google Cloud Tech, published 15 September 2026, presenter Annie Wang. Code: github.com/cuppibla/live-dj. I could not pull the spoken transcript from YouTube, so this analysis is from the video description, chapter list and the linked code repository.

### What she builds

A browser-based voice agent called Mira, a late-night radio DJ you can talk over. Browser mic audio goes over a WebSocket to a Python FastAPI backend, which holds one open connection to the Gemini Live API. Gemini listens and speaks directly (speech in, speech out, no separate speech-to-text or text-to-speech). It is a web demo. There is no phone call or telephony in it.

### The points she makes, and what they mean for us

1. **The speech-to-text, then LLM, then text-to-speech pipeline "feels broken."** Each stage waits for the previous one, so there are pauses, and you cannot talk over the agent naturally. Her fix is a single bidirectional audio stream to a speech-to-speech model.
   For us: this is the same latency problem in section 3 of the requirements doc. The two ways to solve it are a speech-to-speech model (Gemini Live) or a well-tuned streaming cascade (Pipecat with streaming STT and streaming TTS). Both work for phone calls; the cascade needs more tuning.

2. **"A phone call, not a walkie-talkie."** One open connection, audio flowing both directions at the same time, all the time.
   For us: a real phone call already is exactly this. Plivo or Twilio give us a bidirectional WebSocket audio stream per call. We bridge that stream to Gemini Live (or to the cascade). The code shape in her video is the code shape of our call bridge.

3. **Architecture: browser, WebSocket, backend, Gemini Live.**
   For us: replace "browser" with "telephony provider media stream". Everything else stays. Extra work: the phone sends 8 kHz mu-law audio and Gemini wants 16 kHz PCM in and returns 24 kHz PCM out, so the bridge transcodes both directions.

4. **ADK building blocks: Agent (model, personality, tools), Runner (runs the live loop), Session (conversation memory).**
   For us: useful as a mental model. We do not have to use ADK. Pipecat already implements the same loop with telephony transports, and ADK has no phone transport. We may use ADK if we want Google's tooling; it is optional.

5. **LiveRequestQueue decouples the two audio directions so neither blocks the other.**
   For us: same principle in our bridge. Inbound phone audio and outbound model audio run as independent async tasks.

6. **send_realtime for continuous mic audio versus send_content for complete, discrete input** (like a text message or a tool result).
   For us: phone audio is send_realtime. Injecting context mid-call ("the customer's order is X") or a tool result is send_content.

7. **Keep sending audio even in silence.** The model's voice activity detection needs the silence to know the caller has finished, and the stream stays alive.
   For us: never pause the phone-to-model stream, including hold music or dead air.

8. **run_live yields audio, captions, tool calls and an "interrupted" signal.**
   For us: captions become our transcript, tool calls become "look up order", "send WhatsApp link", "mark outcome", and the interrupted signal is how we stop playing audio to the phone when the caller barges in.

9. **The in-memory session gotcha.** Her demo keeps sessions in memory, which is lost on restart and does not scale.
   For us: call state lives in Postgres and Redis from day one.

### The Gemini APIs she uses, and their real cost

| Item | Detail |
|---|---|
| API | Gemini Live API (WebSocket, bidirectional audio) via the `google-genai` Python SDK, and Google ADK on top in episode two |
| Current models | `gemini-3.8-live`, `gemini-3.1-flash-live-preview`, `gemini-2.5-flash-native-audio-preview-12-2025` |
| Audio input price | $3.00 per million tokens, which Google lists as about $0.005 per minute |
| Audio output price | $12.00 per million tokens, about $0.018 per minute |
| Text input / output | $0.75 / $4.50 per million tokens (small for us: the system prompt and tool results) |
| Session limits | Audio-only sessions up to 15 minutes; 128k-token context for native audio models. Fine for a 2 to 5 minute call. |
| Languages | 99 languages including Hindi; switches language mid-conversation automatically |
| Features we need | Built-in voice activity detection with tunable sensitivity, barge-in, function calling, input and output transcription |
| Limitation | Native audio models only output audio. You cannot get text-only replies to feed into ElevenLabs without paying for the audio anyway. |
| Telephony | No native phone support. Community bridges exist for Twilio media streams, and Pipecat ships a Gemini Live service with Twilio transport. |

Cost of a typical 3-minute abandoned cart call on Gemini Live, agent speaking about half the time: about $0.015 audio in, about $0.027 audio out, under $0.005 text. Roughly **$0.045 per call, or Rs 1.3 per minute** before telephony.

---

## 3. Cost comparison of the three ways to build the engine

All figures are per call minute for Indian outbound calls and are estimates from public pricing. Telephony assumes Plivo or Exotel at about Rs 0.8 to 1.0 per minute. Exchange rate assumed Rs 84 per dollar.

| Stack | Ears | Brain | Voice | Engine cost / min | With telephony | Notes |
|---|---|---|---|---|---|---|
| A. ElevenLabs Agents platform | ElevenLabs | Claude or Gemini via platform | ElevenLabs | about Rs 7 to 9 | **Rs 9 to 11** | Fastest to build. Most expensive. |
| B. Custom cascade with ElevenLabs voice | Deepgram (Rs 0.65) or Sarvam (Rs 1.5) | Claude Sonnet 5 (about Rs 1) or Gemini 3.5 Flash-Lite (about Rs 0.2) | ElevenLabs Flash at $0.05 per 1,000 characters, about Rs 2 per call minute | about Rs 3 to 4.5 | **Rs 4 to 5.5** | Keeps ElevenLabs voice. About half the cost of A. |
| C. Gemini Live speech-to-speech | Gemini Live | Gemini Live | Google native voices | about Rs 1.3 | **Rs 2 to 2.5** | Cheapest by far. Voice is Google's, not ElevenLabs. Lowest latency. |

At 10,000 call minutes a month (roughly 3,000 to 4,000 calls):

| Stack | Monthly engine plus telephony |
|---|---|
| A | about Rs 90,000 to 110,000 |
| B | about Rs 40,000 to 55,000 |
| C | about Rs 20,000 to 25,000 |

Plus a server for B and C at about Rs 2,000 to 4,000 a month.

### The honest tradeoff

Your two stated goals pull in opposite directions. ElevenLabs voices sound the best, but the cheapest engine (C) does not use them. My recommendation:

- Build the engine on **Pipecat**, which supports both B and C with a configuration change. The telephony bridge, dialer, webhooks, database and dashboard are identical for both.
- Run the pilot with **both voices on the same script**: Gemini native voice (stack C) and ElevenLabs Flash voice (stack B). Compare answer rate, hang-up rate, and how many customers say yes. Pick by conversion per rupee, not by how nice it sounds to us.
- If a Hindi or Hinglish Gemini native voice sounds natural enough to your customers, stack C wins on cost by a wide margin and is where the volume campaign (abandoned cart) should run. Stack B can still serve calls where the ElevenLabs voice measurably converts better.

Do not build on the ElevenLabs Agents platform (A) given the cost goal. It is 2 to 4 times the cost of B or C and locks the conversation layer.

---

## 4. Tech stack

Chosen for lowest cost, Indian latency, and a small team.

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Pipecat, google-genai, Anthropic and ElevenLabs SDKs are all Python-first |
| Voice engine | Pipecat | Open source. Has Gemini Live, Deepgram, Sarvam-compatible STT, ElevenLabs TTS, Plivo and Twilio transports, turn detection and interruption built in |
| API server | FastAPI + Uvicorn | Webhook receivers (Shopify, GoKwik, telephony events), call control, dashboard API; same as the video |
| Job queue | Redis + a worker (arq or RQ) | Dial scheduler, delays for abandoned cart, retries, post-call jobs |
| Database | PostgreSQL | Campaigns, contacts, calls, transcripts, outcomes, consent, cost per call |
| File storage | S3-compatible bucket in Mumbai | Recordings |
| Telephony | Plivo first (documented audio streaming API, Indian numbers, native SIP to ElevenLabs if ever needed); Exotel as the alternative | Both are 2 to 3 times cheaper than Twilio for India |
| Speech models | Stack C: Gemini Live (`gemini-3.8-live` or the 2.5 native audio preview). Stack B: Deepgram Nova-3 or Sarvam for STT, ElevenLabs Flash v2.5 for TTS | Section 3 |
| Text LLM (stack B and post-call work) | Claude Opus 5 as the quality default; Claude Sonnet 5 or Gemini 3.5 Flash-Lite as the cost option, chosen after measuring on real transcripts | Post-call summaries and outcome extraction run as cheap batch jobs |
| WhatsApp | Meta WhatsApp Cloud API directly, or a BSP such as Interakt or Gupshup | Sending the checkout link after the call is what actually converts |
| Dashboard | Next.js + Tailwind + shadcn/ui, or a simple server-rendered admin | Start with the minimum: calls list, outcomes, recordings, campaign settings |
| Auth | Simple email login for two or three staff; Supabase Auth or Clerk if we want it fast | |
| Hosting | One VPS in Mumbai (DigitalOcean BLR1, AWS ap-south-1, or Hetzner if latency to India is acceptable), Docker Compose | Rs 2,000 to 4,000 a month. Vercel free tier for the dashboard |
| Observability | Structured logs plus per-call latency traces stored in Postgres; Sentry free tier | |
| Repo | GitHub, one monorepo: `engine/`, `api/`, `dashboard/` | |

---

## 5. How a call works end to end

```
Shopify or GoKwik webhook
        |
        v
FastAPI receives event, verifies signature, stores contact + cart/order
        |
        v
Scheduler decides: eligible? consent? calling hours? delay elapsed? not already called?
        |
        v
Worker asks Plivo to dial the customer from our 160/140 number
        |
        v
Customer answers. Plivo opens a WebSocket audio stream to our engine
        |
        v
Pipecat pipeline: phone audio -> Gemini Live (or STT -> LLM -> ElevenLabs) -> phone audio
   tools available: get_order, get_cart, send_whatsapp_link, mark_outcome,
                    schedule_callback, transfer_to_human, end_call
        |
        v
Call ends. Recording + transcript saved. Post-call job extracts outcome, cost, summary
        |
        v
Dashboard shows the call. WhatsApp link already sent if the customer agreed.
```

---

## 6. What I need from you

Create the accounts yourself. Put every key into a local `.env` file in the project folder. Do not paste keys into this chat.

### Accounts and credentials

| Item | Needed for | Notes |
|---|---|---|
| Google AI Studio API key (or a GCP project with Vertex AI enabled) | Gemini Live | Pay-as-you-go billing enabled |
| ElevenLabs API key and the voice IDs you like | Stack B voice | A Creator or Pro plan is enough to start |
| Anthropic API key | Claude for the text brain and post-call summaries | |
| Plivo account with an Indian number | Calling | KYC documents for the business; request a 160-series number for transactional calls and a 140-series for promotional |
| DLT registration (Vil, Jio, Airtel portals) | Legal requirement for commercial calls | Longest lead time. Start now. |
| Shopify custom app with Admin API access token | Order and checkout webhooks, order lookups | Scopes: read_orders, read_checkouts, read_customers, read_products |
| GoKwik point of contact | Abandoned cart webhook | Ask them to send abandoned checkout events to our webhook URL |
| WhatsApp Business API access (Meta Cloud API or a BSP) | Sending checkout links and confirmations | Optional for the first order-confirmation build |
| Deepgram or Sarvam API key | Stack B speech-to-text | Only if we run the ElevenLabs voice variant |
| A VPS or cloud account in Mumbai, and a domain with HTTPS | Hosting the engine and receiving webhooks | I can set up Docker and the reverse proxy |
| GitHub repository | Code | |

### Business inputs

- Product catalogue with the benefit points you want the agent to use per product or category.
- The offer policy: may the agent give a discount to close an abandoned cart? How much? Is there a code?
- Delivery and return policy text so the agent answers correctly.
- Persona: agent name, gender of voice, languages, and the exact disclosure line ("I am an AI assistant calling from ...").
- Calling hours and the human escalation number, if any.
- 20 to 50 recent real orders and abandoned checkouts (with phone numbers) for testing, and permission to call them.
- Any recordings of your existing human calls, if you have them, for tuning the script.

### Decisions

- Confirm order confirmation first, abandoned cart second.
- Confirm Plivo versus Exotel (I recommend Plivo unless you already have an Exotel account).
- Confirm the pilot will test both voices (Gemini native and ElevenLabs).

---

## 7. Compliance notes specific to these two campaigns

- Order confirmation is a service call. Use a 160-series number. No DND scrub needed, but keep the disclosure line and log consent.
- Abandoned cart recovery is closer to promotional. Safest reading of TRAI rules: the customer initiated a checkout in the last 90 days, so a service-style reminder call is defensible, but a pitch with offers is promotional and should go from a 140-series number with DND scrubbing. We will build the campaign so the number series and the DND check are settings, and you decide with your compliance advisor.
- Every call opens with the AI disclosure and offers an opt-out. "Do not call me" marks the contact and blocks future calls immediately.
- Never take card details, UPI PINs or OTPs by voice. The agent sends a link instead.

---

## 8. First two weeks

| Days | Work |
|---|---|
| 1 to 2 | You: create accounts, start DLT, share `.env`, GoKwik request. Me: repo, Docker, Postgres, FastAPI skeleton, Shopify webhook receiver |
| 3 to 5 | Plivo outbound call bridged to Pipecat with Gemini Live. First live test call to your own phone. |
| 6 to 7 | Order confirmation script, tools (get_order, mark_outcome, end_call), transcript and recording storage |
| 8 to 9 | Same pipeline with ElevenLabs voice variant for comparison. Latency tracing per stage. |
| 10 to 12 | Minimal dashboard: calls list, outcome, play recording, campaign settings, calling hours |
| 13 to 14 | 50 real test calls on order confirmation. Review, fix, measure cost per call. Decide voice. |

Abandoned cart follows in weeks 3 and 4 once the GoKwik webhook or the Shopify checkout webhook is live.

---

## Sources

- Video: https://www.youtube.com/watch?v=yQEKMsCtsmE and code https://github.com/cuppibla/live-dj
- Gemini API pricing: https://ai.google.dev/gemini-api/docs/pricing
- Gemini Live API capabilities: https://ai.google.dev/gemini-api/docs/live-guide
- Pipecat with Gemini Live: https://docs.pipecat.ai/pipecat/features/gemini-live
- Add telephony to a Gemini Live agent with Twilio: https://dev.to/googleai/add-telephony-to-a-gemini-live-agent-with-twilio-1elc
- ElevenLabs API pricing: https://elevenlabs.io/pricing/api and https://developer.puter.com/tutorials/elevenlabs-api-pricing/
- GoKwik abandoned checkout webhook setup: https://help.spurnow.com/en/articles/10562030-recover-abandoned-checkouts-with-gokwik-and-spur and https://help.bik.ai/en/articles/8750241-gokwik-integration
- Shopify abandoned checkouts: https://shopify.dev/docs/api/admin-rest/latest/resources/abandoned-checkouts
- Plivo India voice pricing: https://www.plivo.com/voice/pricing/in/
