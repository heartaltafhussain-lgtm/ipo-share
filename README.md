# IPO Share Dashboard

- `template.html` — dashboard design
- `data/ipos.json` — IPO data (auto-updated)
- `scripts/update_ipos.py` — naye IPO add karta hai, subscription (QIB/NII/Retail/Employee) bharta hai, listing ke 30 din baad hata deta hai, aur `index.html` rebuild karta hai
- `.github/workflows/update.yml` — har 3 ghante auto-run

## Setup (GitHub Pages)
1. Ye folder GitHub repo me push karein.
2. Settings → Pages → Source: `main` branch, root.
3. Settings → Actions → General → Workflow permissions: "Read and write".
4. Manual run: Actions → Update IPO Share → Run workflow.

Local run: `python scripts/update_ipos.py`
