# Voice Agent dashboard (Next.js)

The dashboard for the Python server in `../backend`. It has no server of its own: `npm run build` writes plain
files to `out/`, and the Python server serves them at http://127.0.0.1:8000/. All data comes from the Python
API (`/api/...`).

```
npm install      # once
npm run dev      # http://localhost:3000, with /api forwarded to the Python server on port 8000
npm run build    # the version the Python server shows (run.bat does this on every start)
npm run lint
npm run format
```

## Layout

```
src/app/               pages (App Router): page.tsx is the Overview
src/components/layout  sidebar, top bar, search (Ctrl/⌘ K), notifications
src/components/overview  the Overview page's cards, live calls, chart, attention list
src/components/ui      buttons, cards, badges, selects
src/hooks              usePoll (refreshing data), useTheme (dark mode)
src/lib                API types (api.ts), formatting, the sidebar's page list (nav.ts)
```

Pages not rebuilt here yet link to the original dashboard at `/classic#<page>` (see `src/lib/nav.ts`).
Build the pages in this static-export style: client components that fetch `/api`, no server actions, no
dynamic routes without `generateStaticParams` (use `?id=` query strings instead).
