# sir-26-27-4-capture-study
Project 4 — NFC vs. QR vs. PIN: Clock-In Capture Study

Area: K12ERP Time & Attendance v2 
Intern: _______________  Mentor: Ana

Research question: How do NFC badge-tap, QR wall-code scan, and PIN entry compare on speed, error rate, and user friction for school staff clock-in?

Why this matters to APS
T&A v2 chose NTAG213 NFC stickers on badges as primary, QR for satellite sites, and PIN as fallback. Those choices rest on assumptions. Your measurements tell us whether they hold and inform v3 (phone-as-badge).

What you'll deliver
- A kiosk prototype on an Android tablet that supports all three capture methods and logs timing and errors.
- A usability protocol (task script, consent form for adult volunteers, metrics).
- A study with APS staff and other adult volunteers: latency per tap, failure/retry rate, and a short friction survey.
- Findings with recommendations, including whether phone-read NDEF tags perform well enough for v3.

Milestones
When	Milestone
Oct 28	Proposal: protocol, metrics, prototype scope
Dec 16	Prototype capturing all three methods
Feb 24	Study complete; data analyzed
Mar 31	Paper draft and figures
April	IMSAloquium; hand-off

Data & tools
- Android tablet, NTAG213 stickers, printed QR codes. Kotlin/Android or a web kiosk with Web NFC; synthetic staff roster only.
- Software components can start before all kiosk gear is on site. A synthetic roster/punch scaffold is available — see ../synthetic_punch_generator.py.

Skills you'll use and build
Mobile/NFC development, human-subjects study design, statistics, and turning measurements into product decisions.

How Ana will support you
Ana supplies hardware and the kiosk spec, reviews the study protocol, and recruits volunteer participants.

Human-subjects note
You're studying adult volunteers. Use a written consent form, keep responses anonymous, and only collect the timing/error/friction metrics your protocol lists. Ana reviews the protocol and consent form before any session.

Ground rules (all projects)
- Synthetic roster only — no real staff records.
- Sign the NDA day one. Don't share credentials. Only touch what you're provisioned for.
- Use AI tools, but you own every line you commit — understand it and cite AI assistance in your SIR writeup.
- Report to Ana on arrival and before leaving. Tell Ana by Tuesday evening if you'll miss a Wednesday.
- Safety: low-voltage tablets and NFC only. No FANUC or shop equipment without a staff member present.

Repo layout (suggested)
project4-capture-study/
├── README.md
├── NOTEBOOK.md
├── PROPOSAL.md
├── kiosk/               # NFC / QR / PIN capture + timing+error logging
├── protocol/            # task script, consent form, survey
├── data/                # de-identified session logs (synthetic roster)
└── results/             # latency, failure/retry, friction analysis + figures


First-Wednesday checklist
- [ ] NDA signed
- [ ] Sandbox account + this repo access confirmed
- [ ] Environment runs; confirmed whether tablet/stickers are on hand yet (start software either way)
- [ ] Drafted first version of metrics list in NOTEBOOK.md
- [ ] Demo at 3:00 — one concrete thing; commit + push before leaving
