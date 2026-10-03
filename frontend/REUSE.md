# Selective reuse

Source: https://github.com/Kravchenko-Dmitry1980/ai-meeting-secretary, `main`, commit `0268c2bd4cebfbba98e0001b5c75bd70b3e52cd9`, inspected in `Secretary/.reference/ai-meeting-secretary` on 2026-10-01. Repository owner: Kravchenko-Dmitry1980. Reuse was expressly authorized by the owner in the Secretary task.

The donor has **no final open-source license** (`README.md:162-164`); no MIT/Apache license is assigned to its original files. Keep owner provenance when sharing this application. Original selected files have no per-file copyright notices; none have been removed.

| New file | Donor file | Treatment |
|---|---|---|
| `src/utils/cn.ts` | same | Reused helper |
| `src/components/ui/Card.tsx` | same | Reused typed presentation component |
| `src/components/ui/Button.tsx` | same | Adapted sizing, added danger, safe default type |
| `src/components/ui/Badge.tsx` | same | Adapted colors and spacing |
| `src/components/ui/Tabs.tsx` | same | Adapted styling, ARIA tabs |
| `src/components/LogoMark.tsx` | same | Retained layered violet mark; replaced constant animation with audio icon |
| `src/index.css`, `tailwind.config.js` | donor same files | Adapted violet/mint palette and glass panels; newly designed app layout |

MainPage upload/result/tab structure informed the new interaction design. No wholesale MainPage copy, demonstration data, synthetic metrics/testimonials, localStorage secret handling, old polling hook, provider clients, or Docker dependencies were copied. The meeting application and HTTP/SSE flow are new and conform to `docs/IMPLEMENTATION_CONTRACT.md`.

Dependencies have their own licenses: React/React DOM, clsx, tailwind-merge, Tailwind/Vite (MIT); lucide-react (ISC); TypeScript (Apache-2.0). All installed dependency versions and integrity hashes are fixed in `package-lock.json`; see root `THIRD_PARTY_NOTICES.md`.
