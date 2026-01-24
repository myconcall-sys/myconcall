# 📞 Screener Concall Tracker

Automated system that tracks upcoming investor concalls from Screener.in, extracts dial-in numbers from PDFs, and syncs everything to Google Sheets + Calendar.

**Zero manual work. Runs daily at 7 AM IST.**

## ✨ Features

- 🔍 Scrapes 100 upcoming concalls from Screener.in
- 📄 Extracts phone numbers from PDF announcements (92% success rate)
- 📊 Auto-updates Google Sheet with all concall details
- 📅 Creates Google Calendar events with reminders
- 🎨 Color codes calls by watchlist (My Stonks → Tomato, Core Watchlist → Flamingo/Tangerine/Banana)
- 🔔 My Stonks events auto-sync to main calendar
- 📧 Email notifications on success/failure
- ☁️ Runs automatically via GitHub Actions (even if your laptop is off)

## 📊 Live Data

- **Google Sheet:** [View Concalls](https://docs.google.com/spreadsheets/d/1xkvpwd0Sg89Zgi0cJC4Ow_wiz3_N1lWR4izgxFCCiN8)

## ⚙️ How It Works
```
Every day at 7 AM IST:
┌─────────────────┐
│  Screener.in    │
└────────┬────────┘
         ▼
┌─────────────────┐
│  Scrape 100     │
│  Concalls       │
└────────┬────────┘
         ▼
┌─────────────────┐
│  Download PDFs  │
│  Extract Phones │
└────────┬────────┘
         ▼
    ┌────┴────┐
    ▼         ▼
┌───────┐ ┌──────────┐
│ Sheet │ │ Calendar │
└───────┘ └──────────┘
         ▼
┌─────────────────┐
│  Email Summary  │
└─────────────────┘
```

## 🛠️ Tech Stack

- Python + Selenium (web scraping)
- pdfplumber (PDF extraction)
- Google Sheets API
- Google Calendar API
- GitHub Actions (automation)

## 🚀 Built With

This entire project was built in 3 hours using [Claude Code](https://claude.ai) with zero prior coding experience.

## 📝 License

MIT

---

*Built by [@AnishA_Moonka](https://x.com/AnishA_Moonka)*
