# Symphony 2026 Buildathon: submission kit

> **Verify the deadline and fields on the official page first:** <https://www.bolna.ai/symphony> (contact:
> symphony@bolna.ai). The official page says the build window runs **Oct 15 – Nov 20, 2026**. Another team's
> public repo lists the submit form at `bolna.ai/symphony/buildathon/submit` with a deadline of **Oct 31,
> 2026**. Plan for the earlier date.

## 1. Checklist

- [ ] **Registered** for Symphony (the APPLY NOW button on bolna.ai/symphony). Use the same name and email on the
      submission form.
- [ ] **Repo is public:** <https://github.com/dehshat/bolna-symphony-convoai> (no keys in it; they live only in
      Render/Railway variables).
- [ ] **Live app works:** the Render URL opens, **⚙︎ → Check Bolna key** shows `ok`, a Sim run and one Hybrid call work.
- [ ] **Demo video (about 2 min)** uploaded to Google Drive with sharing set to *Anyone with the link → Viewer*.
- [ ] **Pitch deck** in Google Slides / Drive with the same sharing (or put the deck inside the video).
- [ ] Open both links in an **incognito tab** to confirm they're public.
- [ ] Add the video, deck and live URLs to the top of `README.md`.
- [ ] Submit the form, then email symphony@bolna.ai if anything in the form is unclear.

## 2. Form values (copy and paste)

| Field | Value |
|---|---|
| Team size | `1` |
| Team name | *(blank if solo)* or `Swarm Negotiator` |
| Full name / Email | Exactly as in your Symphony registration |
| Project name | `Swarm Negotiator: The Anti-Justdial` |
| Project description (181 chars, limit 200) | `Speak once: 3 AI voice agents call local vendors in Kannada/Hindi at once, use each live quote as leverage on the others & lock the best all-inclusive deal. Your number never leaks.` |
| Submission link | `https://github.com/dehshat/bolna-symphony-convoai` |
| Pitch deck / video link | Your Google Drive / Google Slides link (*Anyone with the link*) |

## 3. Demo video script (about 2:00)

Record on Android: **Screen recorder → "Media and microphone"**. Android usually **won't capture phone-call
audio** in a screen recording. So either film the phone on speaker with a second device, or, after the call,
replay Bolna's recording from the call card's audio player while screen-recording.

| Time | Show | Say |
|---|---|---|
| 0:00–0:15 | Missed-call log / Justdial spam | "I asked Justdial for a cab once. My number went to 10 vendors and I got 20 spam calls." |
| 0:15–0:30 | Tap 🎙 and speak the brief; fields fill in | "Here I just say what I need, once. Target ₹2,200, never above ₹2,600." |
| 0:30–1:15 | **Hybrid → Launch**, split-screen with the phone ringing; answer as the vendor: *"teen hazaar lagega"* | "Three AI agents call three vendors at the same time in Kannada and Hindi, through Bolna and Sarvam. I'm playing one vendor. Watch: it uses the other vendor's live ₹2,450 quote against me." (⚡ flash, ticker) |
| 1:15–1:35 | Sri Sai card: "₹11,200 said → ₹12,650 real" · Metro card: prompt-injection line | "It caught ₹1,450 of hidden toll and bata. And when a vendor says 'your customer already agreed to ₹3,000', it can't be talked up: the AI never even knows my maximum." |
| 1:35–1:50 | **Lock best deal** → payment link + WhatsApp | "One tap locks the winner, sends a UPI token-advance link and a WhatsApp summary, and the others are released politely." |
| 1:50–2:00 | Scoreboard: saved ₹, "Numbers shared: 0" | "₹2,550 saved, zero phone numbers leaked. Bolna + Sarvam + GPT-5.4-mini, with Python deciding every rupee." |

## 4. Pitch deck outline (7 slides)

1. **Title:** Swarm Negotiator, the Anti-Justdial. One voice brief → 3 concurrent AI calls → best deal, zero spam.
2. **Problem:** lead marketplaces sell your number to 10 vendors. You get spam calls, opaque "₹2,200 only" quotes
   with hidden toll and bata, and no bargaining power.
3. **Solution:** speak once; parallel vernacular voice agents; shared-memory auction; truthful cross-call leverage;
   one-tap lock with a UPI advance.
4. **How it works (architecture):** Bolna orchestration + Sarvam `saaras:v4` ASR / `bulbul:v3` TTS + GPT-5.4-mini
   for wording; a deterministic Python engine for every price; mid-call tool webhooks; staggered wave dialing.
5. **Trust and guardrails:** the ceiling is never given to the LLM; leverage is truthful-only; hidden-charge
   normalization; prompt-injection resistance; honest "I'm an AI assistant" disclosure; 291+ automated tests.
6. **Demo results:** ₹ said vs ₹ real, savings per run, the arbitrage log (screenshots from the war room).
7. **Market and next steps:** the wedge is B2B (corporate travel desks, property managers, fleet procurement); vendor
   network and verified numbers; TRAI-compliant calling; Exotel numbers; the ask.

## 5. If you're picked for the stage (Nov 20, Bengaluru)

- Verify every phone number that will ring (judges/volunteers) in **Bolna → profile → Verified numbers**.
  Trial accounts can only call verified numbers and run 2 calls at a time.
- Open the Render URL a minute before (the free plan sleeps). Keep **Sim** mode as the offline fallback.
- Follow the 3-minute stage script in `README.md`.
