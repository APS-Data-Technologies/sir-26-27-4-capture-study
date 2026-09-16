# PROJECT BRIEF 4 — NFC vs. QR vs. PIN: Clock-In Capture Study

**Area:** K12ERP Time & Attendance v2

> This is the official project assignment from the IMSA SIR Internship Handbook (2026–2027). Treat it as the source of truth for what this project is. Keep it unchanged; track your own progress and notes in the README and NOTEBOOK.

## Research question
How do NFC badge-tap, QR wall-code scan, and PIN entry compare on speed, error rate, and user friction for school staff clock-in?

## Why this matters to APS
T&A v2 chose NTAG213 NFC stickers on badges as primary, QR for satellite sites, and PIN as fallback. Those choices rest on assumptions. Your measurements tell us whether they hold and inform v3 (phone-as-badge).

## What you will deliver
- A kiosk prototype on an Android tablet that supports all three capture methods and logs timing and errors.
- A usability protocol (task script, consent form for adult volunteers, metrics).
- A study with APS staff and other adult volunteers: latency per tap, failure/retry rate, and a short friction survey.
- Findings with recommendations, including whether phone-read NDEF tags perform well enough for v3.

## Milestones
| When | Milestone |
|------|-----------|
| Oct 28 | Proposal: protocol, metrics, prototype scope |
| Dec 16 | Prototype capturing all three methods |
| Feb 24 | Study complete; data analyzed |
| Mar 31 | Paper draft and figures |
| April | IMSAloquium; hand-off |

## Data and tools
Android tablet, NTAG213 stickers, printed QR codes. Kotlin/Android or a web kiosk with Web NFC; synthetic staff roster only.

## Skills you will use and build
Mobile/NFC development, human-subjects study design, statistics, turning measurements into product decisions.

## How Ana will support you
Ana supplies hardware and the kiosk spec, reviews the study protocol, and recruits volunteer participants.
